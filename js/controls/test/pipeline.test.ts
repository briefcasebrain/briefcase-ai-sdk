import { createHash } from "node:crypto";

import { describe, expect, it } from "vitest";

import {
  runSuggestionPipeline,
  sha256Fingerprint,
  type SuggestionPipelineConfig,
} from "../src/pipeline.js";
import type { CacheEntry, QuotaDecision } from "../src/ports.js";

interface Item {
  title: string;
}

class FakeCache {
  entry: CacheEntry<Item> | null;
  reads: Array<Record<string, unknown>> = [];
  writes: Array<Record<string, unknown>> = [];

  constructor(entry: CacheEntry<Item> | null = null) {
    this.entry = entry;
  }

  read(args: Record<string, unknown>): CacheEntry<Item> | null {
    this.reads.push(args);
    return this.entry;
  }

  write(args: Record<string, unknown>): void {
    this.writes.push(args);
  }
}

class FakeQuota {
  allowed: boolean;
  cooldowns: Array<Record<string, unknown>> = [];

  constructor(allowed = true) {
    this.allowed = allowed;
  }

  acquire(): QuotaDecision {
    return { allowed: this.allowed, tokensRemaining: this.allowed ? 4 : 0 };
  }

  markCooldown(args: Record<string, unknown>): void {
    this.cooldowns.push(args);
  }
}

function makeConfig(
  overrides: Partial<SuggestionPipelineConfig<Item>> = {},
): SuggestionPipelineConfig<Item> & { cache: FakeCache; quotaStore: FakeQuota } {
  return {
    tenantId: "org1",
    scopeId: "strand1",
    kind: "kpi_suggestion",
    ttlHours: 24,
    bucket: "kpi_suggestion",
    cache: new FakeCache(),
    quotaStore: new FakeQuota(),
    isEnabled: () => true,
    call: async () => ({ items: [{ title: "from-bedrock" }], usage: null }),
    buildFallback: () => [{ title: "template" }],
    ...overrides,
  } as SuggestionPipelineConfig<Item> & { cache: FakeCache; quotaStore: FakeQuota };
}

describe("step 1: cache read", () => {
  it("serves a fresh bedrock-sourced row as a hit", async () => {
    const cache = new FakeCache({
      entryId: "e1",
      items: [{ title: "cached" }],
      source: "bedrock",
    });
    const config = makeConfig({ cache });
    let called = false;
    config.call = async () => {
      called = true;
      return { items: [], usage: null };
    };
    const result = await runSuggestionPipeline(config);
    expect(result).toEqual({ items: [{ title: "cached" }], cached: true, source: "cache" });
    expect(called).toBe(false);
    expect(cache.writes).toEqual([]);
  });

  it("never serves a stored template_fallback row as a hit", async () => {
    const cache = new FakeCache({
      entryId: "e1",
      items: [{ title: "template" }],
      source: "template_fallback:flag_off",
    });
    const result = await runSuggestionPipeline(makeConfig({ cache }));
    expect(result.source).toBe("bedrock");
    expect(result.cached).toBe(false);
  });

  it("ignores rows with no items", async () => {
    const cache = new FakeCache({ entryId: "e1", items: [], source: "bedrock" });
    const result = await runSuggestionPipeline(makeConfig({ cache }));
    expect(result.source).toBe("bedrock");
  });

  it("misses when the configured fingerprint differs", async () => {
    const cache = new FakeCache({
      entryId: "e1",
      items: [{ title: "cached" }],
      source: "bedrock",
      fingerprint: "old-doc",
    });
    const result = await runSuggestionPipeline(
      makeConfig({ cache, cacheFingerprint: "new-doc" }),
    );
    expect(result.source).toBe("bedrock");
  });

  it("misses when the row predates fingerprints", async () => {
    const cache = new FakeCache({
      entryId: "e1",
      items: [{ title: "cached" }],
      source: "bedrock",
    });
    const result = await runSuggestionPipeline(
      makeConfig({ cache, cacheFingerprint: "new-doc" }),
    );
    expect(result.source).toBe("bedrock");
  });

  it("hits when the fingerprint matches", async () => {
    const cache = new FakeCache({
      entryId: "e1",
      items: [{ title: "cached" }],
      source: "bedrock",
      fingerprint: "doc",
    });
    const result = await runSuggestionPipeline(makeConfig({ cache, cacheFingerprint: "doc" }));
    expect(result.source).toBe("cache");
  });

  it("ignores stored fingerprints when none is configured", async () => {
    const cache = new FakeCache({
      entryId: "e1",
      items: [{ title: "cached" }],
      source: "bedrock",
      fingerprint: "doc",
    });
    const result = await runSuggestionPipeline(makeConfig({ cache }));
    expect(result.source).toBe("cache");
  });

  it("passes ttlHours and the injected clock to the store", async () => {
    const cache = new FakeCache(null);
    const now = new Date(1_700_000_000_000);
    await runSuggestionPipeline(makeConfig({ cache, clock: () => now }));
    expect(cache.reads[0]).toMatchObject({
      tenantId: "org1",
      scopeId: "strand1",
      kind: "kpi_suggestion",
      ttlHours: 24,
      now,
    });
  });
});

describe("step 2: flag check", () => {
  it("falls back with flag_off and writes a template row", async () => {
    const config = makeConfig({ isEnabled: () => false });
    let capChecked = false;
    config.entitlements = {
      isHardCapped: () => {
        capChecked = true;
        return false;
      },
    };
    const result = await runSuggestionPipeline(config);
    expect(result).toEqual({
      items: [{ title: "template" }],
      cached: false,
      source: "template_fallback",
      fallbackReason: "flag_off",
    });
    expect(capChecked).toBe(false);
    expect(config.cache.writes[0]).toMatchObject({
      items: [{ title: "template" }],
      source: "template_fallback:flag_off",
    });
  });
});

describe("step 3: entitlements hard cap", () => {
  it("falls back with credits_exhausted before touching quota", async () => {
    let acquired = false;
    const quota = new FakeQuota();
    quota.acquire = () => {
      acquired = true;
      return { allowed: true, tokensRemaining: 4 };
    };
    const result = await runSuggestionPipeline(
      makeConfig({ quotaStore: quota, entitlements: { isHardCapped: () => true } }),
    );
    expect(result.source).toBe("template_fallback");
    expect(result.fallbackReason).toBe("credits_exhausted");
    expect(acquired).toBe(false);
  });
});

describe("step 4: quota", () => {
  it("falls back with quota_exhausted when denied", async () => {
    const config = makeConfig({ quotaStore: new FakeQuota(false) });
    let called = false;
    config.call = async () => {
      called = true;
      return { items: [], usage: null };
    };
    const result = await runSuggestionPipeline(config);
    expect(result.fallbackReason).toBe("quota_exhausted");
    expect(called).toBe(false);
  });
});

describe("step 5: provider call", () => {
  it("returns bedrock items and writes a bedrock row", async () => {
    const config = makeConfig();
    const result = await runSuggestionPipeline(config);
    expect(result).toEqual({
      items: [{ title: "from-bedrock" }],
      cached: false,
      source: "bedrock",
    });
    expect(config.cache.writes).toHaveLength(1);
    expect(config.cache.writes[0]).toMatchObject({
      tenantId: "org1",
      scopeId: "strand1",
      kind: "kpi_suggestion",
      items: [{ title: "from-bedrock" }],
      source: "bedrock",
    });
  });

  it("drops the fingerprint key from writes when not configured", async () => {
    const config = makeConfig();
    await runSuggestionPipeline(config);
    expect("fingerprint" in config.cache.writes[0]!).toBe(false);
  });

  it("persists the fingerprint when configured", async () => {
    const config = makeConfig({ cacheFingerprint: "doc" });
    await runSuggestionPipeline(config);
    expect(config.cache.writes[0]).toMatchObject({ source: "bedrock", fingerprint: "doc" });
  });

  it("passes actorId and an app-side rowId to writes", async () => {
    const config = makeConfig({ actorId: "user1", rowId: () => "row-42" });
    await runSuggestionPipeline(config);
    expect(config.cache.writes[0]).toMatchObject({ actorId: "user1", rowId: "row-42" });
  });

  it("captures usage through the sink", async () => {
    const captures: Array<Record<string, unknown>> = [];
    const config = makeConfig({
      usageSink: {
        capture: (args) => {
          captures.push(args as Record<string, unknown>);
        },
      },
      call: async () => ({
        items: [{ title: "x" }],
        usage: { inputTokens: 11, outputTokens: 7, model: "nova-micro" },
      }),
    });
    await runSuggestionPipeline(config);
    expect(captures).toEqual([
      {
        tenantId: "org1",
        bucket: "kpi_suggestion",
        model: "nova-micro",
        inputTokens: 11,
        outputTokens: 7,
        scopeId: "strand1",
        ctx: undefined,
      },
    ]);
  });

  it("skips the sink when usage is null", async () => {
    let captured = false;
    const config = makeConfig({
      usageSink: {
        capture: () => {
          captured = true;
        },
      },
    });
    await runSuggestionPipeline(config);
    expect(captured).toBe(false);
  });

  it("awaits usage capture when awaitUsageCapture is set", async () => {
    let settled = false;
    const config = makeConfig({
      awaitUsageCapture: true,
      usageSink: {
        capture: () =>
          new Promise<void>((resolve) =>
            setTimeout(() => {
              settled = true;
              resolve();
            }, 10),
          ),
      },
      call: async () => ({
        items: [{ title: "x" }],
        usage: { inputTokens: 1, outputTokens: 1, model: "m" },
      }),
    });
    await runSuggestionPipeline(config);
    expect(settled).toBe(true);
  });

  it("fire-and-forgets usage capture by default", async () => {
    let settled = false;
    const config = makeConfig({
      usageSink: {
        capture: () =>
          new Promise<void>((resolve) =>
            setTimeout(() => {
              settled = true;
              resolve();
            }, 20),
          ),
      },
      call: async () => ({
        items: [{ title: "x" }],
        usage: { inputTokens: 1, outputTokens: 1, model: "m" },
      }),
    });
    await runSuggestionPipeline(config);
    expect(settled).toBe(false);
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(settled).toBe(true);
  });
});

describe("step 6: failure fallbacks", () => {
  it("falls back with bedrock_failed on a plain provider error", async () => {
    const config = makeConfig({
      call: async () => {
        throw new Error("model exploded");
      },
    });
    const result = await runSuggestionPipeline(config);
    expect(result.fallbackReason).toBe("bedrock_failed");
    expect(config.quotaStore.cooldowns).toEqual([]);
  });

  it("marks a cooldown on a throttled provider error, still bedrock_failed", async () => {
    const config = makeConfig({
      call: async () => {
        throw { name: "ThrottlingException", message: "slow down" };
      },
    });
    const result = await runSuggestionPipeline(config);
    expect(result.fallbackReason).toBe("bedrock_failed");
    expect(config.quotaStore.cooldowns).toEqual([
      { tenantId: "org1", bucket: "kpi_suggestion", seconds: 3600, ctx: undefined },
    ]);
  });

  it("honors cooldownSeconds on throttle", async () => {
    const config = makeConfig({
      cooldownSeconds: 60,
      call: async () => {
        throw { name: "TooManyRequestsException" };
      },
    });
    await runSuggestionPipeline(config);
    expect(config.quotaStore.cooldowns[0]).toMatchObject({ seconds: 60 });
  });

  it("fails closed when buildFallback returns null", async () => {
    const config = makeConfig({
      buildFallback: () => null,
      call: async () => {
        throw new Error("model exploded");
      },
    });
    const result = await runSuggestionPipeline(config);
    expect(result).toEqual({
      items: [],
      cached: false,
      source: "failed",
      failureCode: "bedrock_failed",
    });
    expect(config.cache.writes[0]).toMatchObject({ items: [], source: "failed:bedrock_failed" });
  });
});

describe("sha256Fingerprint", () => {
  it("matches node:crypto sha256 hex", () => {
    expect(sha256Fingerprint("doc body")).toBe(
      createHash("sha256").update("doc body", "utf8").digest("hex"),
    );
  });
});
