import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import {
  ChainConflictError,
  GENESIS_PRIOR_HASH,
  HashChainAppender,
  canonicalJson,
  computeEntryHash,
  computePayloadHash,
  verifyChainSegment,
  type HashChainEntry,
  type HashChainStore,
} from "../src/integrity/index.js";

const fixture = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/integrity_golden.json", import.meta.url), "utf8"),
) as {
  chain: {
    payload: Record<string, unknown>;
    payload_hash: string;
    entry_hash: string;
    canonical_payload_hex: string;
  };
};

describe("integrity", () => {
  it("matches the Python canonical JSON and payload hash golden vector", () => {
    expect(Buffer.from(canonicalJson(fixture.chain.payload)).toString("hex")).toBe(
      fixture.chain.canonical_payload_hex,
    );
    expect(computePayloadHash(fixture.chain.payload)).toBe(fixture.chain.payload_hash);
  });

  it("matches the Python spec-v1 entry hash golden vector", () => {
    expect(
      computeEntryHash({
        rowId: "01234567-89ab-7def-8000-000000000001",
        table: "findings",
        entityId: "entity-1",
        observedAt: new Date("2026-01-01T00:00:00.000Z"),
        recordedAt: new Date("2026-01-01T00:00:01.001Z"),
        payloadHash: fixture.chain.payload_hash,
        supersedes: null,
        priorHash: GENESIS_PRIOR_HASH,
      }),
    ).toBe(fixture.chain.entry_hash);
  });

  it("rejects non-finite numbers and unsupported JSON values", () => {
    expect(() => canonicalJson({ value: Number.NaN })).toThrow(/finite/);
    expect(() => canonicalJson({ value: undefined } as never)).toThrow(/unsupported/);
  });

  it("reports the first broken prior hash or entry hash", () => {
    const entry: HashChainEntry = {
      rowId: "row-1",
      table: "events",
      entityId: null,
      observedAt: new Date("2026-01-01T00:00:00Z"),
      recordedAt: new Date("2026-01-01T00:00:00Z"),
      payloadHash: computePayloadHash({ ok: true }),
      supersedes: null,
      priorHash: GENESIS_PRIOR_HASH,
      hash: "bad",
      signature: null,
    };
    expect(verifyChainSegment([entry])).toEqual({ ok: false, rowId: "row-1", reason: "hash" });
    expect(verifyChainSegment([{ ...entry, priorHash: "11".repeat(32) }])).toEqual({
      ok: false,
      rowId: "row-1",
      reason: "prior_hash",
    });
  });

  it("retries a compare-and-swap conflict against the refreshed tail", async () => {
    const entries: HashChainEntry[] = [];
    let conflicts = 0;
    const store: HashChainStore = {
      lastEntryHash: () => entries.at(-1)?.hash ?? GENESIS_PRIOR_HASH,
      append(entry) {
        if (conflicts++ === 0) throw new ChainConflictError("raced");
        entries.push(entry);
      },
    };
    const appender = new HashChainAppender(store, { sleep: async () => undefined });
    const entry = await appender.appendRow({
      table: "events",
      rowId: "row-1",
      entityId: null,
      observedAt: new Date("2026-01-01T00:00:00Z"),
      recordedAt: new Date("2026-01-01T00:00:00Z"),
      payload: { ok: true },
    });
    expect(entry.hash).toHaveLength(64);
    expect(conflicts).toBe(2);
  });

  it("throws the final conflict after the configured attempt bound", async () => {
    const store: HashChainStore = {
      lastEntryHash: () => GENESIS_PRIOR_HASH,
      append: () => {
        throw new ChainConflictError("contended");
      },
    };
    const appender = new HashChainAppender(store, { sleep: async () => undefined });
    await expect(
      appender.appendRow({
        table: "events",
        rowId: "row-1",
        entityId: null,
        observedAt: new Date("2026-01-01T00:00:00Z"),
        recordedAt: new Date("2026-01-01T00:00:00Z"),
        payload: { ok: true },
        maxAttempts: 2,
      }),
    ).rejects.toThrow(ChainConflictError);
  });
});
