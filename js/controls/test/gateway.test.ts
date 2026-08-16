import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { collapseLegacyReason, createGateway, type GatewayConfig, type GatewayOutcome } from "../src/gateway.js";
import { MemoryExporter } from "../src/exporters.js";
import type { QuotaDecision } from "../src/ports.js";
import {
  CONTROLS_GATEWAY_BUCKET,
  CONTROLS_GATEWAY_OUTCOME,
  CONTROLS_GATEWAY_TENANT_ID,
  CONTROLS_QUOTA_COOLDOWN_UNTIL,
  CONTROLS_QUOTA_TOKENS_REMAINING,
} from "../src/semconv.js";

class FakeQuotaStore {
  allowed: boolean;
  tokensRemaining: number;
  acquires: Array<[string, string, Record<string, unknown>]> = [];
  cooldowns: Array<[string, string, number]> = [];

  constructor(allowed = true, tokensRemaining = 5) {
    this.allowed = allowed;
    this.tokensRemaining = tokensRemaining;
  }

  acquire(args: { tenantId: string; bucket: string; policy: Record<string, unknown> }): QuotaDecision {
    this.acquires.push([args.tenantId, args.bucket, { ...args.policy }]);
    return { allowed: this.allowed, tokensRemaining: this.tokensRemaining };
  }

  markCooldown(args: { tenantId: string; bucket: string; seconds: number }): void {
    this.cooldowns.push([args.tenantId, args.bucket, args.seconds]);
  }
}

const BUCKETS = { chat: { capacity: 5, refillSecondsPerToken: 60 } };

function makeGateway(overrides: Partial<GatewayConfig> = {}) {
  const store = new FakeQuotaStore();
  const config: GatewayConfig = { quotaStore: store, buckets: BUCKETS, ...overrides };
  return { gateway: createGateway(config), store: (config.quotaStore ?? store) as FakeQuotaStore };
}

describe("gateway composition", () => {
  it("returns the value and tokens remaining on success", async () => {
    const { gateway, store } = makeGateway();
    const outcome = await gateway.invoke({ tenantId: "org1", bucket: "chat", fn: async () => "answer" });
    expect(outcome).toMatchObject({ ok: true, value: "answer", tokensRemaining: 5 });
    expect(store.acquires).toEqual([["org1", "chat", BUCKETS.chat]]);
  });

  it("supports sync fns", async () => {
    const { gateway } = makeGateway();
    const outcome = await gateway.invoke({ tenantId: "org1", bucket: "chat", fn: () => 42 });
    expect(outcome).toMatchObject({ ok: true, value: 42 });
  });

  it("hard cap short-circuits before quota", async () => {
    const { gateway, store } = makeGateway({ entitlements: { isHardCapped: () => true } });
    const outcome = await gateway.invoke({ tenantId: "org1", bucket: "chat", fn: () => 1 });
    expect(outcome).toMatchObject({ ok: false, reason: "hard_capped", tokensRemaining: 0 });
    expect(store.acquires).toEqual([]);
  });

  it("quota denial returns quota_exhausted without running the fn", async () => {
    const ran: number[] = [];
    const { gateway } = makeGateway({ quotaStore: new FakeQuotaStore(false, 0) });
    const outcome = await gateway.invoke({
      tenantId: "org1",
      bucket: "chat",
      fn: () => ran.push(1),
    });
    expect(outcome).toMatchObject({ ok: false, reason: "quota_exhausted", tokensRemaining: 0 });
    expect(ran).toEqual([]);
  });

  it("marks a cooldown on throttle and reports it", async () => {
    const { gateway, store } = makeGateway({ cooldownSeconds: 1800 });
    const cause = { name: "ThrottlingException", message: "slow down" };
    const outcome = await gateway.invoke({
      tenantId: "org1",
      bucket: "chat",
      fn: () => {
        throw cause;
      },
    });
    expect(outcome).toMatchObject({ ok: false, reason: "throttled", tokensRemaining: 0, cause });
    expect(store.cooldowns).toEqual([["org1", "chat", 1800]]);
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) expect(outcome.cooldownUntil).toBeInstanceOf(Date);
  });

  it("swallows markCooldown failures after a throttle", async () => {
    const store = new FakeQuotaStore();
    store.markCooldown = () => {
      throw new Error("db down");
    };
    const { gateway } = makeGateway({ quotaStore: store });
    const outcome = await gateway.invoke({
      tenantId: "org1",
      bucket: "chat",
      fn: () => {
        throw { name: "ThrottlingException" };
      },
    });
    expect(outcome).toMatchObject({ ok: false, reason: "throttled" });
  });

  it("carries the cause on internal errors", async () => {
    const boom = new Error("boom");
    const { gateway } = makeGateway();
    const outcome = await gateway.invoke({
      tenantId: "org1",
      bucket: "chat",
      fn: () => {
        throw boom;
      },
    });
    expect(outcome).toMatchObject({ ok: false, reason: "internal", cause: boom });
  });

  it("rejects unknown buckets", async () => {
    const { gateway } = makeGateway();
    await expect(
      gateway.invoke({ tenantId: "org1", bucket: "nope", fn: () => 1 }),
    ).rejects.toThrow(/bucket/i);
  });

  it("rejects invalid portErrorPolicy at construction", () => {
    expect(() =>
      createGateway({
        quotaStore: new FakeQuotaStore(),
        buckets: BUCKETS,
        portErrorPolicy: "explode" as GatewayConfig["portErrorPolicy"],
      }),
    ).toThrow(/portErrorPolicy/);
  });

  it("propagates port errors by default", async () => {
    const { gateway } = makeGateway({
      entitlements: {
        isHardCapped: () => {
          throw new Error("db down");
        },
      },
    });
    await expect(
      gateway.invoke({ tenantId: "org1", bucket: "chat", fn: () => 1 }),
    ).rejects.toThrow("db down");
  });

  it("port error policy allow continues", async () => {
    const { gateway } = makeGateway({
      entitlements: {
        isHardCapped: () => {
          throw new Error("db down");
        },
      },
      portErrorPolicy: "allow",
    });
    const outcome = await gateway.invoke({ tenantId: "org1", bucket: "chat", fn: () => 7 });
    expect(outcome).toMatchObject({ ok: true, value: 7 });
  });

  it("port error policy deny blocks as hard_capped", async () => {
    const { gateway } = makeGateway({
      entitlements: {
        isHardCapped: () => {
          throw new Error("db down");
        },
      },
      portErrorPolicy: "deny",
    });
    const outcome = await gateway.invoke({ tenantId: "org1", bucket: "chat", fn: () => 7 });
    expect(outcome).toMatchObject({ ok: false, reason: "hard_capped" });
  });

  it("supports async quota stores", async () => {
    const cooldowns: number[] = [];
    const { gateway } = makeGateway({
      quotaStore: {
        acquire: async () => ({ allowed: true, tokensRemaining: 9 }),
        markCooldown: async ({ seconds }: { seconds: number }) => {
          cooldowns.push(seconds);
        },
      },
    });
    const outcome = await gateway.invoke({ tenantId: "org1", bucket: "chat", fn: () => 1 });
    expect(outcome).toMatchObject({ ok: true, tokensRemaining: 9 });
  });

  it("passes an AbortSignal derived from deadlineMs to the fn", async () => {
    const { gateway } = makeGateway();
    const outcome = await gateway.invoke({
      tenantId: "org1",
      bucket: "chat",
      deadlineMs: 10,
      fn: (signal) =>
        new Promise((_resolve, reject) => {
          signal!.addEventListener("abort", () => reject(new Error("deadline hit")));
        }),
    });
    expect(outcome).toMatchObject({ ok: false, reason: "internal" });
    if (!outcome.ok && outcome.reason === "internal") {
      expect(String((outcome.cause as Error).message)).toBe("deadline hit");
    }
  });
});

describe("gateway records and events", () => {
  it("exports a record with the wire schema fields", async () => {
    const mem = new MemoryExporter();
    const { gateway } = makeGateway({ exporter: mem, awaitExport: true });
    await gateway.invoke({ tenantId: "org1", bucket: "chat", fn: () => "v" });
    expect(mem.records).toHaveLength(1);
    const rec = mem.records[0] as Record<string, unknown>;
    for (const field of [
      "decision_id",
      "decision_type",
      "function_name",
      "inputs",
      "outputs",
      "started_at",
      "ended_at",
      "execution_time_ms",
    ]) {
      expect(rec).toHaveProperty(field);
    }
    expect(rec["decision_type"]).toBe("controls.gateway.chat");
    expect((rec["inputs"] as Record<string, unknown>)[CONTROLS_GATEWAY_TENANT_ID]).toBe("org1");
    expect((rec["inputs"] as Record<string, unknown>)[CONTROLS_GATEWAY_BUCKET]).toBe("chat");
    const outputs = rec["outputs"] as Record<string, unknown>;
    expect(outputs[CONTROLS_GATEWAY_OUTCOME]).toBe("ok");
    expect(outputs[CONTROLS_QUOTA_TOKENS_REMAINING]).toBe(5);
    expect(outputs[CONTROLS_QUOTA_COOLDOWN_UNTIL]).toBeNull();
  });

  it("never exports fn result content", async () => {
    const mem = new MemoryExporter();
    const { gateway } = makeGateway({ exporter: mem, awaitExport: true });
    await gateway.invoke({ tenantId: "org1", bucket: "chat", fn: () => "secret-payload" });
    expect(JSON.stringify(mem.records)).not.toContain("secret-payload");
  });

  it("records the cause class name on failure", async () => {
    const mem = new MemoryExporter();
    const { gateway } = makeGateway({ exporter: mem, awaitExport: true });
    await gateway.invoke({
      tenantId: "org1",
      bucket: "chat",
      fn: () => {
        throw new RangeError("out of range");
      },
    });
    const rec = mem.records[0] as Record<string, unknown>;
    expect(rec["error"]).toBe("RangeError");
    expect(JSON.stringify(rec)).not.toContain("out of range");
  });

  it("fires onOutcome with the default event name and payload", async () => {
    const events: Array<[string, Record<string, unknown>]> = [];
    const { gateway } = makeGateway({
      onOutcome: (name, payload) => events.push([name, payload]),
    });
    await gateway.invoke({ tenantId: "org1", bucket: "chat", fn: () => 1 });
    expect(events).toHaveLength(1);
    const [name, payload] = events[0]!;
    expect(name).toBe("gateway_ok");
    expect(payload["bucket"]).toBe("chat");
    expect(payload["tenantId"]).toBe("org1");
  });

  it("uses gateway_throttled for throttle outcomes", async () => {
    const events: string[] = [];
    const { gateway } = makeGateway({ onOutcome: (name) => void events.push(name) });
    await gateway.invoke({
      tenantId: "org1",
      bucket: "chat",
      fn: () => {
        throw { name: "ThrottlingException" };
      },
    });
    expect(events).toEqual(["gateway_throttled"]);
  });

  it("event names are injectable", async () => {
    const events: string[] = [];
    const { gateway } = makeGateway({
      eventNames: { ok: "ai_invoke_ok" },
      onOutcome: (name) => void events.push(name),
    });
    await gateway.invoke({ tenantId: "org1", bucket: "chat", fn: () => 1 });
    expect(events).toEqual(["ai_invoke_ok"]);
  });

  it("survives a throwing onOutcome callback", async () => {
    const { gateway } = makeGateway({
      onOutcome: () => {
        throw new Error("observer bug");
      },
    });
    const outcome = await gateway.invoke({ tenantId: "org1", bucket: "chat", fn: () => 1 });
    expect(outcome).toMatchObject({ ok: true });
  });
});

describe("collapseLegacyReason (legacy outcome-shape parity)", () => {
  const NOW = 1_700_000_000_000;

  it("passes success through", () => {
    const outcome: GatewayOutcome<string> = { ok: true, value: "v", tokensRemaining: 4 };
    const legacy = collapseLegacyReason(outcome);
    expect(legacy).toEqual({ ok: true, value: "v", tokensRemaining: 4 });
    expect(Object.keys(legacy).sort()).toEqual(["ok", "tokensRemaining", "value"]);
  });

  it("collapses hard_capped to the exact legacy shape", () => {
    const legacy = collapseLegacyReason({ ok: false, reason: "hard_capped", tokensRemaining: 0 });
    expect(legacy).toEqual({
      ok: false,
      reason: "quota_exhausted",
      tokensRemaining: 0,
      cooldownUntil: null,
    });
    expect(Object.keys(legacy).sort()).toEqual([
      "cooldownUntil",
      "ok",
      "reason",
      "tokensRemaining",
    ]);
  });

  it("passes quota_exhausted through with its cooldown", () => {
    const until = new Date(NOW + 1000);
    const legacy = collapseLegacyReason({
      ok: false,
      reason: "quota_exhausted",
      tokensRemaining: 2,
      cooldownUntil: until,
    });
    expect(legacy).toEqual({
      ok: false,
      reason: "quota_exhausted",
      tokensRemaining: 2,
      cooldownUntil: until,
    });
  });

  it("collapses throttled to quota_exhausted with a recomputed cooldown", () => {
    const legacy = collapseLegacyReason(
      {
        ok: false,
        reason: "throttled",
        tokensRemaining: 0,
        cooldownUntil: new Date(NOW - 5000),
        cause: { name: "ThrottlingException" },
      },
      { cooldownSeconds: 3600, now: () => NOW },
    );
    expect(legacy).toEqual({
      ok: false,
      reason: "quota_exhausted",
      tokensRemaining: 0,
      cooldownUntil: new Date(NOW + 3600 * 1000),
    });
  });

  it("keeps internal errors as internal with the cause", () => {
    const cause = new Error("boom");
    const legacy = collapseLegacyReason({ ok: false, reason: "internal", cause });
    expect(legacy).toEqual({ ok: false, reason: "internal", cause });
    expect(Object.keys(legacy).sort()).toEqual(["cause", "ok", "reason"]);
  });
});

describe("outcome surface parity with tests/fixtures/gateway_outcomes.json", () => {
  const fixture = JSON.parse(
    readFileSync(new URL("../../../tests/fixtures/gateway_outcomes.json", import.meta.url), "utf8"),
  ) as { outcomes: Array<{ key: string; event: string; record_error: string | null }> };

  class ThrottlingException extends Error {
    constructor() {
      super("rate exceeded");
      this.name = "ThrottlingException";
    }
  }

  // Drives the gateway to one outcome. causeClass is the expected error
  // class name on the exported record, or null when no error is recorded.
  function driverFor(key: string): {
    overrides: Partial<GatewayConfig>;
    fn: () => unknown;
    causeClass: string | null;
  } {
    switch (key) {
      case "ok":
        return { overrides: {}, fn: () => "value", causeClass: null };
      case "hard_capped":
        return {
          overrides: { entitlements: { isHardCapped: () => true } },
          fn: () => 1,
          causeClass: null,
        };
      case "quota_exhausted":
        return {
          overrides: { quotaStore: new FakeQuotaStore(false, 0) },
          fn: () => 1,
          causeClass: null,
        };
      case "throttled":
        return {
          overrides: {},
          fn: () => {
            throw new ThrottlingException();
          },
          causeClass: "ThrottlingException",
        };
      case "internal":
        return {
          overrides: {},
          fn: () => {
            throw new RangeError("out of range");
          },
          causeClass: "RangeError",
        };
      default:
        throw new Error(`No driver for outcome key ${JSON.stringify(key)}`);
    }
  }

  it("fixture covers exactly the gateway's outcome keys", () => {
    expect(fixture.outcomes.map((o) => o.key).sort()).toEqual([
      "hard_capped",
      "internal",
      "ok",
      "quota_exhausted",
      "throttled",
    ]);
  });

  it.each(fixture.outcomes)("default event name for $key matches the fixture", async ({ key, event }) => {
    const { overrides, fn } = driverFor(key);
    const events: string[] = [];
    const { gateway } = makeGateway({ ...overrides, onOutcome: (name) => void events.push(name) });
    await gateway.invoke({ tenantId: "org1", bucket: "chat", fn });
    expect(events).toEqual([event]);
  });

  it.each(fixture.outcomes)(
    "exported record error for $key matches the fixture",
    async ({ key, record_error }) => {
      const { overrides, fn, causeClass } = driverFor(key);
      const mem = new MemoryExporter();
      const { gateway } = makeGateway({ ...overrides, exporter: mem, awaitExport: true });
      await gateway.invoke({ tenantId: "org1", bucket: "chat", fn });
      expect(mem.records).toHaveLength(1);
      const rec = mem.records[0] as Record<string, unknown>;
      expect((rec["outputs"] as Record<string, unknown>)[CONTROLS_GATEWAY_OUTCOME]).toBe(key);
      if (record_error === null) {
        expect(causeClass).toBeNull();
        expect(rec).not.toHaveProperty("error");
      } else {
        expect(rec["error"]).toBe(causeClass);
      }
    },
  );
});
