import { randomBytes } from "node:crypto";

import { describe, expect, it, vi } from "vitest";

import {
  ConnectorRefreshError,
  createAesGcmEnvelope,
  createOAuthState,
  runConnectorSync,
  verifyOAuthState,
  type ConnectorConnection,
  type ConnectorResource,
  type ConnectorStore,
} from "../src/connectors/index.js";

interface Resource extends ConnectorResource {
  value: number;
}

function connection(overrides: Partial<ConnectorConnection> = {}): ConnectorConnection {
  return {
    id: "connection-1",
    tenantId: "tenant-1",
    provider: "example",
    accessToken: "old-access",
    refreshToken: "refresh",
    tokenExpiresAt: new Date(0),
    refreshVersion: "v1",
    refreshedAt: null,
    ...overrides,
  };
}

function store(initial = connection()): ConnectorStore {
  let current = initial;
  return {
    beginRun: vi.fn(async () => "run-1"),
    loadConnection: vi.fn(async () => current),
    latestCursor: vi.fn(async () => null),
    compareAndSwapTokens: vi.fn(async ({ expectedRefreshVersion, tokens }) => {
      if (current.refreshVersion !== expectedRefreshVersion) return false;
      current = {
        ...current,
        accessToken: tokens.accessToken,
        refreshToken: tokens.refreshToken ?? current.refreshToken,
        tokenExpiresAt: tokens.expiresAt ?? null,
        refreshVersion: "v2",
        refreshedAt: new Date("2026-01-01T00:00:00Z"),
      };
      return true;
    }),
    completeRun: vi.fn(async () => undefined),
    failRun: vi.fn(async () => undefined),
    markNeedsReconnect: vi.fn(async () => undefined),
  };
}

describe("connector runner", () => {
  it("skips refresh while the access token is outside the expiry skew", async () => {
    const state = store(connection({ tokenExpiresAt: new Date("2026-01-01T01:00:00Z") }));
    const refresh = vi.fn();
    const result = await runConnectorSync<Resource>({
      tenantId: "tenant-1",
      connectionId: "connection-1",
      provider: "example",
      store: state,
      strategy: { provider: "example", refreshAccessToken: refresh, fetchResources: async () => [] },
      sink: { write: async () => ({ written: 0 }) },
      now: () => new Date("2026-01-01T00:00:00Z"),
    });
    expect(refresh).not.toHaveBeenCalled();
    expect(result).toEqual({ runId: "run-1", fetched: 0, written: 0 });
  });

  it("refreshes expired tokens and caps the cursor at run start", async () => {
    const state = store();
    const resources: Resource[] = [
      { externalId: "a", occurredAt: new Date("2025-12-31T23:59:00Z"), value: 1 },
      { externalId: "future", occurredAt: new Date("2026-01-01T00:01:00Z"), value: 2 },
    ];
    const result = await runConnectorSync<Resource>({
      tenantId: "tenant-1",
      connectionId: "connection-1",
      provider: "example",
      store: state,
      strategy: {
        provider: "example",
        refreshAccessToken: async () => ({ accessToken: "new", refreshToken: "new-refresh", expiresInSeconds: 3600 }),
        fetchResources: async ({ accessToken }) => {
          expect(accessToken).toBe("new");
          return resources;
        },
      },
      sink: { write: async (items) => ({ written: items.length }) },
      now: () => new Date("2026-01-01T00:00:00Z"),
    });
    expect(result.written).toBe(2);
    expect(state.completeRun).toHaveBeenCalledWith(
      expect.objectContaining({ cursor: "2025-12-31T23:59:00.000Z" }),
    );
  });

  it("uses the run-start cursor for an empty run", async () => {
    const state = store(connection({ tokenExpiresAt: new Date("2026-01-01T01:00:00Z") }));
    const times = [
      new Date("2026-01-01T00:00:00Z"),
      new Date("2026-01-01T00:00:05Z"),
    ];
    await runConnectorSync<Resource>({
      tenantId: "tenant-1",
      connectionId: "connection-1",
      provider: "example",
      store: state,
      strategy: { provider: "example", fetchResources: async () => [] },
      sink: { write: async () => ({ written: 0 }) },
      now: () => times.shift() ?? new Date("2026-01-01T00:00:05Z"),
    });
    expect(state.completeRun).toHaveBeenCalledWith(
      expect.objectContaining({ cursor: "2026-01-01T00:00:00.000Z" }),
    );
  });

  it("does not regress an existing cursor when a batch contains only older resources", async () => {
    const state = store(connection({ tokenExpiresAt: new Date("2026-01-01T01:00:00Z") }));
    vi.mocked(state.latestCursor).mockResolvedValue("2025-12-31T23:59:00.000Z");
    await runConnectorSync<Resource>({
      tenantId: "tenant-1",
      connectionId: "connection-1",
      provider: "example",
      store: state,
      strategy: {
        provider: "example",
        fetchResources: async () => [
          { externalId: "old", occurredAt: new Date("2025-12-31T23:58:00Z"), value: 1 },
        ],
      },
      sink: { write: async () => ({ written: 1 }) },
      now: () => new Date("2026-01-01T00:00:00Z"),
    });
    expect(state.completeRun).toHaveBeenCalledWith(
      expect.objectContaining({ cursor: "2025-12-31T23:59:00.000Z" }),
    );
  });

  it("loads the winning connection after losing the token compare-and-swap", async () => {
    const winner = connection({ accessToken: "winner", refreshVersion: "v2" });
    const state = store();
    vi.mocked(state.compareAndSwapTokens).mockResolvedValueOnce(false);
    vi.mocked(state.loadConnection).mockResolvedValueOnce(connection()).mockResolvedValueOnce(winner);
    await runConnectorSync<Resource>({
      tenantId: "tenant-1",
      connectionId: "connection-1",
      provider: "example",
      store: state,
      strategy: {
        provider: "example",
        refreshAccessToken: async () => ({ accessToken: "loser" }),
        fetchResources: async ({ accessToken }) => {
          expect(accessToken).toBe("winner");
          return [];
        },
      },
      sink: { write: async () => ({ written: 0 }) },
      now: () => new Date("2026-01-01T00:00:00Z"),
    });
  });

  it("writes each page from a paginated strategy before fetching the next", async () => {
    const connectorStore = store(connection({ tokenExpiresAt: new Date("2026-01-01T01:00:00Z") }));
    const order: string[] = [];
    const sink = {
      write: vi.fn(async (resources: readonly Resource[]) => {
        order.push(`write:${resources.map((resource) => resource.externalId).join(",")}`);
        return { written: resources.length };
      }),
    };
    const page = (...values: number[]): Resource[] =>
      values.map((value) => ({ externalId: `r${value}`, occurredAt: new Date(`2025-12-0${value}T00:00:00Z`), value }));
    const result = await runConnectorSync<Resource>({
      tenantId: "tenant-1",
      connectionId: "connection-1",
      provider: "example",
      store: connectorStore,
      sink,
      now: () => new Date("2026-01-01T00:00:00Z"),
      strategy: {
        provider: "example",
        async *fetchResources() {
          for (const values of [[1, 2], [], [4, 3]]) {
            order.push(`fetch:${values.length}`);
            yield page(...values);
          }
        },
      },
    });
    expect(order).toEqual(["fetch:2", "write:r1,r2", "fetch:0", "fetch:2", "write:r4,r3"]);
    expect(result).toEqual({ runId: "run-1", fetched: 4, written: 4 });
    expect(connectorStore.completeRun).toHaveBeenCalledWith(
      expect.objectContaining({ fetched: 4, written: 4, cursor: "2025-12-04T00:00:00.000Z" }),
    );
  });

  it("fails the run without advancing the cursor when a later page throws", async () => {
    const connectorStore = store(connection({ tokenExpiresAt: new Date("2026-01-01T01:00:00Z") }));
    const sink = { write: vi.fn(async (resources: readonly Resource[]) => ({ written: resources.length })) };
    await expect(runConnectorSync<Resource>({
      tenantId: "tenant-1",
      connectionId: "connection-1",
      provider: "example",
      store: connectorStore,
      sink,
      now: () => new Date("2026-01-01T00:00:00Z"),
      strategy: {
        provider: "example",
        async *fetchResources() {
          yield [{ externalId: "r1", occurredAt: new Date("2025-12-01T00:00:00Z"), value: 1 }];
          throw new Error("page 2 unavailable");
        },
      },
    })).rejects.toThrow("page 2 unavailable");
    expect(sink.write).toHaveBeenCalledTimes(1);
    expect(connectorStore.completeRun).not.toHaveBeenCalled();
    expect(connectorStore.failRun).toHaveBeenCalledWith(expect.objectContaining({ error: "page 2 unavailable" }));
  });

  it("rethrows the original error when failRun itself throws", async () => {
    const connectorStore = store(connection({ tokenExpiresAt: new Date("2026-01-01T01:00:00Z") }));
    const failRunError = new Error("run table unavailable");
    connectorStore.failRun = vi.fn(async () => { throw failRunError; });
    const original = new Error("provider exploded");
    const onFailRunError = vi.fn();
    const run = (extra: object) => runConnectorSync<Resource>({
      tenantId: "tenant-1",
      connectionId: "connection-1",
      provider: "example",
      store: connectorStore,
      sink: { write: vi.fn(async () => ({ written: 0 })) },
      strategy: { provider: "example", fetchResources: async () => { throw original; } },
      ...extra,
    });
    await expect(run({ onFailRunError })).rejects.toBe(original);
    expect(onFailRunError).toHaveBeenCalledWith(failRunError, original);
    await expect(run({})).rejects.toBe(original);
    await expect(run({ onFailRunError: () => { throw new Error("observer bug"); } })).rejects.toBe(original);
  });

  it("marks a terminal refresh failure for reconnect and records the failed run", async () => {
    const state = store();
    await expect(
      runConnectorSync<Resource>({
        tenantId: "tenant-1",
        connectionId: "connection-1",
        provider: "example",
        store: state,
        strategy: {
          provider: "example",
          refreshAccessToken: async () => {
            throw new ConnectorRefreshError("grant revoked", "terminal");
          },
          fetchResources: async () => [],
        },
        sink: { write: async () => ({ written: 0 }) },
        now: () => new Date("2026-01-01T00:00:00Z"),
      }),
    ).rejects.toThrow("grant revoked");
    expect(state.markNeedsReconnect).toHaveBeenCalledOnce();
    expect(state.failRun).toHaveBeenCalledOnce();
  });
});

describe("connector security", () => {
  it("round-trips PKCE state and rejects expiry or a mismatched nonce", () => {
    const minted = createOAuthState({ tenantId: "tenant-1", context: "property-1", now: 1_000 });
    expect(verifyOAuthState({ state: minted.state, cookieValue: minted.cookieValue, now: 1_001 })).toEqual({
      ok: true,
      tenantId: "tenant-1",
      codeVerifier: minted.codeVerifier,
      context: "property-1",
    });
    expect(verifyOAuthState({ state: minted.state, cookieValue: minted.cookieValue, now: 1_000 + 600_001 })).toEqual({
      ok: false,
      reason: "expired",
    });
    const other = createOAuthState({ tenantId: "tenant-2", now: 1_000 });
    expect(verifyOAuthState({ state: minted.state, cookieValue: other.cookieValue, now: 1_001 })).toEqual({
      ok: false,
      reason: "bad_nonce",
    });
  });

  it("uses a fresh IV and binds ciphertext to caller-supplied AAD", async () => {
    const key = randomBytes(32);
    const envelope = createAesGcmEnvelope({ keyProvider: async () => key });
    const first = await envelope.seal("secret", "tenant-1|provider|access");
    const second = await envelope.seal("secret", "tenant-1|provider|access");
    expect(first).not.toBe(second);
    await expect(envelope.open(first, "tenant-1|provider|access")).resolves.toBe("secret");
    await expect(envelope.open(first, "tenant-2|provider|access")).rejects.toThrow();
  });

  it("rejects malformed envelopes and non-256-bit keys", async () => {
    const badKey = createAesGcmEnvelope({ keyProvider: () => Buffer.alloc(16) });
    await expect(badKey.seal("x", "aad")).rejects.toThrow(/32 bytes/);
    const valid = createAesGcmEnvelope({ keyProvider: () => Buffer.alloc(32) });
    await expect(valid.open("v2.bad", "aad")).rejects.toThrow(/malformed/);
  });
});
