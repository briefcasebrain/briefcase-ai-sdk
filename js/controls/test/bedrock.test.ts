import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import {
  BEDROCK_DEFAULT_RATE,
  BEDROCK_MODEL_ALIASES,
  BEDROCK_RATES,
  BEDROCK_RATE_PER_TOKENS,
  __resetForTests,
  createCredentialProvider,
  createMemoizedCredentialProvider,
  normalizeBedrockModelId,
  priceForUsage,
  rateForModel,
  resolveModelId,
  supportsConverseTools,
  supportsPriorityServiceTier,
} from "../src/bedrock/index.js";

describe("resolveModelId cascade", () => {
  it("prefers the env override", () => {
    expect(
      resolveModelId({ envOverride: "override", profilePrefix: "profile", fallback: "fb" }),
    ).toBe("override");
  });

  it("falls through an empty-string override to the profile prefix", () => {
    expect(resolveModelId({ envOverride: "", profilePrefix: "profile", fallback: "fb" })).toBe(
      "profile",
    );
  });

  it("falls through empty override and prefix to the fallback", () => {
    expect(resolveModelId({ envOverride: "", profilePrefix: "", fallback: "fb" })).toBe("fb");
  });

  it("treats missing values like empty strings", () => {
    expect(resolveModelId({ fallback: "fb" })).toBe("fb");
  });

  it("prefers the feature override over the env override", () => {
    expect(
      resolveModelId({
        featureOverride: "feature",
        envOverride: "override",
        profilePrefix: "profile",
        fallback: "fb",
      }),
    ).toBe("feature");
  });

  it("falls through an empty-string feature override to the env override", () => {
    expect(
      resolveModelId({ featureOverride: "", envOverride: "override", fallback: "fb" }),
    ).toBe("override");
  });
});

describe("supportsConverseTools", () => {
  it.each([
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "global.amazon.nova-pro-v1:0",
    "anthropic.claude-3-haiku-20240307-v1:0",
    "cohere.command-r-plus-v1:0",
    "mistral.mistral-large-2407-v1:0",
    "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123",
  ])("accepts %s", (id) => {
    expect(supportsConverseTools(id)).toBe(true);
  });

  it.each([
    "meta.llama3-70b-instruct-v1:0",
    "amazon.titan-text-express-v1",
    "eu.anthropic.claude-3-haiku-20240307-v1:0",
    "mistral.mistral-small-2402-v1:0",
  ])("rejects %s", (id) => {
    expect(supportsConverseTools(id)).toBe(false);
  });
});

describe("supportsPriorityServiceTier", () => {
  it.each([
    "us.amazon.nova-pro-v1:0",
    "global.amazon.nova-premier-v1:0",
    "amazon.nova-pro",
    "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.amazon.nova-pro-v1:0",
  ])("accepts %s", (id) => {
    expect(supportsPriorityServiceTier(id)).toBe(true);
  });

  it.each([
    "us.amazon.nova-micro-v1:0",
    "amazon.nova-lite-v1:0",
    "amazon.nova-proxy-v1:0",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123",
  ])("rejects %s", (id) => {
    expect(supportsPriorityServiceTier(id)).toBe(false);
  });
});

describe("normalizeBedrockModelId", () => {
  it.each([
    ["us.anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5"],
    ["eu.anthropic.claude-3-haiku-20240307-v1:0", "claude-3-haiku"],
    ["apac.amazon.nova-lite-v1:0", "nova-lite"],
    ["global.amazon.nova-pro-v1:0", "nova-pro"],
    ["amazon.titan-embed-text-v2:0", "titan-embed-text"],
    ["mistral.mistral-large-2407-v1:0", "mistral-large-2407"],
    ["meta.llama3-1-70b-instruct-v1:0", "llama3-1-70b-instruct"],
    ["cohere.embed-english-v3", "embed-english-v3"],
    ["claude-sonnet-4-5", "claude-sonnet-4-5"],
  ])("normalizes %s to %s", (input, expected) => {
    expect(normalizeBedrockModelId(input)).toBe(expected);
  });
});

/** Per-1M-token rates registered in the Rust cost engine, keyed by model name. */
function rustEngineRates(): Map<string, { input: number; output: number }> {
  const source = readFileSync(
    new URL("../../../crates/briefcase-core/src/cost.rs", import.meta.url),
    "utf8",
  );
  const rates = new Map<string, { input: number; output: number }>();
  const number = "([0-9][0-9_]*(?:\\.[0-9]+)?)";
  const parse = (text: string) => Number(text.replaceAll("_", ""));
  // ModelPricing::from_mtok("name", "provider", input_per_mtok, output_per_mtok, ...)
  const perMillion = new RegExp(`from_mtok\\(\\s*"([^"]+)",\\s*"[^"]+",\\s*${number},\\s*${number},`, "g");
  for (const [, model, input, output] of source.matchAll(perMillion)) {
    rates.set(model!, { input: parse(input!), output: parse(output!) });
  }
  // ModelPricing { model_name: "name", ..., input_cost_per_1k_tokens: x, output_cost_per_1k_tokens: y }
  const perThousand = new RegExp(
    `model_name:\\s*"([^"]+)"\\.to_string\\(\\),\\s*provider:[^,]+,\\s*` +
      `input_cost_per_1k_tokens:\\s*${number},\\s*output_cost_per_1k_tokens:\\s*${number},`,
    "g",
  );
  for (const [, model, input, output] of source.matchAll(perThousand)) {
    if (!rates.has(model!)) rates.set(model!, { input: parse(input!) * 1000, output: parse(output!) * 1000 });
  }
  return rates;
}

describe("pricing table parity with the Rust cost engine", () => {
  const engine = rustEngineRates();

  it("finds the engine's table", () => {
    expect(engine.size).toBeGreaterThan(20);
    expect(engine.get("claude-opus-4-1")).toEqual({ input: 15, output: 75 });
  });

  it.each(BEDROCK_RATES.map((rate) => [rate.model, rate] as const))(
    "prices %s exactly as the engine does",
    (model, rate) => {
      const expected = engine.get(model);
      expect(expected, `${model} is not registered in cost.rs`).toBeDefined();
      expect(rate.input).toBeCloseTo(expected!.input, 9);
      expect(rate.output).toBeCloseTo(expected!.output, 9);
    },
  );

  it("has unique models and aliases that point at priced models", () => {
    const models = BEDROCK_RATES.map((rate) => rate.model);
    expect(new Set(models).size).toBe(models.length);
    for (const [alias, target] of Object.entries(BEDROCK_MODEL_ALIASES)) {
      expect(models, alias).toContain(target);
      expect(models, alias).not.toContain(alias);
    }
  });

  describe("shared fixture (tests/fixtures/bedrock_rates.json)", () => {
    const fixture = JSON.parse(
      readFileSync(new URL("../../../tests/fixtures/bedrock_rates.json", import.meta.url), "utf8"),
    ) as {
      per_tokens: number;
      rates: Array<{ model: string; input: number; output: number }>;
      aliases: Record<string, string>;
      fallback_rate_model: string;
      opt_in_fallback_rate: { input: number; output: number };
    };

    it("is the same per-model table, row for row", () => {
      expect(BEDROCK_RATE_PER_TOKENS).toBe(fixture.per_tokens);
      expect(fixture.rates).toEqual(BEDROCK_RATES);
      expect(fixture.aliases).toEqual(BEDROCK_MODEL_ALIASES);
    });

    it("has no regex family rows", () => {
      for (const row of fixture.rates) {
        expect(Object.keys(row).sort()).toEqual(["input", "model", "output"]);
        expect(row.model).toMatch(/^[a-z0-9.-]+$/);
      }
    });

    it("holds no price the engine does not register", () => {
      for (const row of fixture.rates) {
        const expected = engine.get(row.model);
        expect(expected, `${row.model} is not registered in cost.rs`).toBeDefined();
        expect(row.input).toBeCloseTo(expected!.input, 9);
        expect(row.output).toBeCloseTo(expected!.output, 9);
      }
      expect(fixture.opt_in_fallback_rate).toEqual(BEDROCK_DEFAULT_RATE);
      expect(engine.get(fixture.fallback_rate_model)).toEqual(fixture.opt_in_fallback_rate);
    });

    it.each(fixture.rates.map((row) => [row.model, row] as const))("prices %s from the fixture row", (model, row) => {
      expect(priceForUsage(model, 1_000_000, 0)).toBeCloseTo(row.input, 9);
      expect(priceForUsage(model, 0, 1_000_000)).toBeCloseTo(row.output, 9);
    });
  });
});

describe("priceForUsage", () => {
  it("prices sonnet at 3/15 per 1M", () => {
    expect(priceForUsage("us.anthropic.claude-sonnet-4-5-20250929-v1:0", 1_000_000, 1_000_000)).toBe(
      18,
    );
  });

  it("prices nova-micro fractions", () => {
    expect(priceForUsage("amazon.nova-micro-v1:0", 2_000_000, 0)).toBeCloseTo(0.07, 10);
  });

  it("prices each Claude model at its own rate, not its family's", () => {
    const perMillion = (model: string) => [priceForUsage(model, 1_000_000, 0), priceForUsage(model, 0, 1_000_000)];
    expect(perMillion("us.anthropic.claude-opus-4-1-20250805-v1:0")).toEqual([15, 75]);
    expect(perMillion("global.anthropic.claude-opus-4-5-20251101-v1:0")).toEqual([5, 25]);
    expect(perMillion("anthropic.claude-opus-4-6-v1")).toEqual([5, 25]);
    expect(perMillion("anthropic.claude-opus-4-7")).toEqual([5, 25]);
    expect(perMillion("anthropic.claude-3-opus-20240229-v1:0")).toEqual([15, 75]);
    expect(perMillion("us.anthropic.claude-haiku-4-5-20251001-v1:0")).toEqual([1, 5]);
    expect(perMillion("anthropic.claude-3-5-haiku-20241022-v1:0")).toEqual([0.8, 4]);
    expect(perMillion("anthropic.claude-3-haiku-20240307-v1:0")).toEqual([0.25, 1.25]);
    expect(perMillion("anthropic.claude-3-5-sonnet-20241022-v2:0")).toEqual([3, 15]);
    expect(perMillion("us.amazon.nova-premier-v1:0")).toEqual([2.5, 12.5]);
  });

  it("returns undefined for a model without a known rate", () => {
    for (const model of [
      "mystery-model",
      "OPUS-TEST",
      "claude-opus-sonnet-hybrid",
      "anthropic.claude-opus-9-9-v1:0",
      "anthropic.claude-sonnet-4-20250514-v1:0",
      "meta.llama3-70b-instruct-v1:0",
      "",
    ]) {
      expect(priceForUsage(model, 1_000_000, 1_000_000), model).toBeUndefined();
      expect(rateForModel(model), model).toBeUndefined();
    }
  });

  it("uses a fallback rate only when the caller opts in", () => {
    const fallback = { fallbackRate: BEDROCK_DEFAULT_RATE };
    const price: number = priceForUsage("mystery-model", 1_000_000, 1_000_000, fallback);
    expect(price).toBe(18);
    // A known model ignores the fallback.
    expect(priceForUsage("anthropic.claude-opus-4-7", 1_000_000, 1_000_000, fallback)).toBe(30);
  });

  it("resolves ids case-insensitively, through ARNs and inference profiles", () => {
    expect(priceForUsage("US.Anthropic.Claude-Haiku-4-5-20251001-v1:0", 1_000_000, 0)).toBe(1);
    expect(
      priceForUsage(
        "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.amazon.nova-pro-v1:0",
        1_000_000,
        0,
      ),
    ).toBeCloseTo(0.8, 10);
    expect(rateForModel("nova-lite")?.model).toBe("nova-lite");
  });

  it("prices embeddings with zero output cost", () => {
    expect(priceForUsage("cohere.embed-english-v3", 1_000_000, 1_000_000)).toBeCloseTo(0.1, 10);
    expect(priceForUsage("cohere.embed-multilingual-v3", 1_000_000, 1_000_000)).toBeCloseTo(0.1, 10);
    expect(priceForUsage("amazon.titan-embed-text-v2:0", 1_000_000, 1_000_000)).toBeCloseTo(0.1, 10);
  });
});

describe("createMemoizedCredentialProvider", () => {
  const failingImporter = () =>
    Promise.reject(new Error("Cannot find module '@aws-sdk/credential-providers'"));

  it("rejects with a peer-dependency hint when the SDK is absent", async () => {
    __resetForTests();
    const getProvider = createMemoizedCredentialProvider({ importer: failingImporter });
    await expect(getProvider()).rejects.toThrow(/@aws-sdk\/credential-providers/);
  });

  it("builds the provider chain once and memoizes it", async () => {
    __resetForTests();
    let imports = 0;
    const chain = { chain: true };
    const importer = async () => {
      imports += 1;
      return { fromNodeProviderChain: () => chain };
    };
    const getProvider = createMemoizedCredentialProvider({ importer });
    expect(await getProvider()).toBe(chain);
    expect(await getProvider()).toBe(chain);
    expect(await createMemoizedCredentialProvider({ importer })()).toBe(chain);
    expect(imports).toBe(1);
  });

  it("does not cache a failed import", async () => {
    __resetForTests();
    let attempts = 0;
    const importer = () => {
      attempts += 1;
      return Promise.reject(new Error("no module"));
    };
    const getProvider = createMemoizedCredentialProvider({ importer });
    await expect(getProvider()).rejects.toThrow(/@aws-sdk\/credential-providers/);
    await expect(getProvider()).rejects.toThrow(/@aws-sdk\/credential-providers/);
    expect(attempts).toBe(2);
  });

  it("does not cache a provider chain failure", async () => {
    __resetForTests();
    let calls = 0;
    const chain = { chain: true };
    const importer = async () => ({
      fromNodeProviderChain: () => {
        calls += 1;
        if (calls === 1) throw new Error("no region configured");
        return chain;
      },
    });
    const getProvider = createMemoizedCredentialProvider({ importer });
    await expect(getProvider()).rejects.toThrow("no region configured");
    expect(await getProvider()).toBe(chain);
    expect(calls).toBe(2);
  });

  it("recovers through the reset hook", async () => {
    __resetForTests();
    const failing = createMemoizedCredentialProvider({ importer: failingImporter });
    await expect(failing()).rejects.toThrow(/@aws-sdk\/credential-providers/);
    __resetForTests();
    const chain = { chain: true };
    const working = createMemoizedCredentialProvider({
      importer: async () => ({ fromNodeProviderChain: () => chain }),
    });
    expect(await working()).toBe(chain);
    __resetForTests();
  });
});

describe("createCredentialProvider (sync-shaped)", () => {
  it("returns a provider function synchronously and fetches lazily", async () => {
    __resetForTests();
    let chainCalls = 0;
    let fetches = 0;
    const importer = async () => ({
      fromNodeProviderChain: () => {
        chainCalls += 1;
        return async () => {
          fetches += 1;
          return { accessKeyId: "AKIA-TEST" };
        };
      },
    });
    const provider = createCredentialProvider({ importer });
    expect(typeof provider).toBe("function");
    expect(chainCalls).toBe(0);
    const creds = (await provider()) as { accessKeyId: string };
    expect(creds.accessKeyId).toBe("AKIA-TEST");
    await provider();
    expect(chainCalls).toBe(1);
    expect(fetches).toBe(2);
  });
});
