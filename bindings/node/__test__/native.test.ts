import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { CostCalculator, normalizeModelId } from "../index.js";

describe("normalizeModelId", () => {
  it("strips platform qualifiers", () => {
    const cases: Array<[string, string]> = [
      ["us.amazon.nova-pro-v1:0", "nova-pro"],
      ["us.amazon.nova-micro-v1:0", "nova-micro"],
      ["eu.anthropic.claude-sonnet-4-6", "claude-sonnet-4-6"],
      ["us.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5"],
      ["amazon.titan-embed-text-v2:0", "titan-embed-text"],
      ["gpt-4o", "gpt-4o"],
    ];
    for (const [raw, expected] of cases) {
      expect(normalizeModelId(raw), raw).toBe(expected);
    }
  });
});

describe("CostCalculator", () => {
  const calc = new CostCalculator();

  it("prices platform-qualified ids identically to canonical ids", () => {
    const direct = calc.estimateCost("nova-pro", 100_000, 1_000);
    const qualified = calc.estimateCost("us.amazon.nova-pro-v1:0", 100_000, 1_000);
    expect(qualified.totalCost).toBe(direct.totalCost);
    expect(direct.totalCost).toBeCloseTo(0.08 + 0.0032, 9);
  });

  it("rejects unknown models with the id in the message", () => {
    expect(() => calc.estimateCost("not-a-model", 10, 10)).toThrow(/not-a-model/);
  });

  it("lists registered model ids", () => {
    const ids = calc.modelIds();
    expect(ids).toContain("nova-pro");
    expect(ids).toContain("claude-sonnet-4-6");
  });

  it("matches the shared bedrock rate fixture for the models it registers", () => {
    const fixture = JSON.parse(
      readFileSync(join(__dirname, "../../../tests/fixtures/bedrock_rates.json"), "utf8"),
    ) as { rates: Array<{ match: string; input: number; output: number }> };
    // Representative canonical ids per fixture family. Claude families are
    // priced per-model in the engine (more precise than the fixture's
    // coarse regex table), so parity is asserted on the models registered
    // at Bedrock list rates.
    const representatives: Record<string, string> = {
      "nova-pro": "nova-pro",
      "nova-lite": "nova-lite",
      "nova-micro": "nova-micro",
      "(cohere|embed|titan-embed)": "titan-embed-text",
    };
    for (const rate of fixture.rates) {
      const model = representatives[rate.match];
      if (!model) continue;
      const est = calc.estimateCost(model, 1_000, rate.output > 0 ? 1_000 : 0);
      const expected =
        (1_000 * rate.input) / 1_000_000 +
        ((rate.output > 0 ? 1_000 : 0) * rate.output) / 1_000_000;
      expect(est.totalCost, model).toBeCloseTo(expected, 9);
    }
  });
});
