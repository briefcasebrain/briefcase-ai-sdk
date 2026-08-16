import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import {
  BEDROCK_DEFAULT_RATE,
  BEDROCK_RATES,
  BEDROCK_RATE_PER_TOKENS,
  __resetForTests,
  createCredentialProvider,
  createMemoizedCredentialProvider,
  normalizeBedrockModelId,
  priceForUsage,
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

describe("pricing table parity with the shared fixture", () => {
  const fixture = JSON.parse(
    readFileSync(new URL("../../../tests/fixtures/bedrock_rates.json", import.meta.url), "utf8"),
  ) as {
    per_tokens: number;
    rates: Array<{ match: string; input: number; output: number }>;
    default: { input: number; output: number };
  };

  it("matches the fixture rates in order and value", () => {
    expect([...BEDROCK_RATES]).toEqual(fixture.rates);
  });

  it("matches the fixture default and denominator", () => {
    expect(BEDROCK_DEFAULT_RATE).toEqual(fixture.default);
    expect(BEDROCK_RATE_PER_TOKENS).toBe(fixture.per_tokens);
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

  it("applies the default when nothing matches", () => {
    expect(priceForUsage("mystery-model", 1_000_000, 1_000_000)).toBe(18);
  });

  it("takes the first match in table order", () => {
    expect(priceForUsage("claude-opus-sonnet-hybrid", 1_000_000, 1_000_000)).toBe(90);
  });

  it("matches case-insensitively", () => {
    expect(priceForUsage("OPUS-TEST", 1_000_000, 0)).toBe(15);
  });

  it("prices embeddings with zero output cost", () => {
    expect(priceForUsage("cohere.embed-english-v3", 1_000_000, 1_000_000)).toBeCloseTo(0.1, 10);
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
