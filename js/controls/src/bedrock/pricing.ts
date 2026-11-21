/**
 * Bedrock list prices, USD per 1M tokens (us-east-1), per model.
 *
 * Every rate is copied from the Rust cost engine's table
 * (crates/briefcase-core/src/cost.rs); a test reads that file and asserts
 * each entry here matches it. Model ids are resolved the way the engine
 * resolves them: region and vendor prefixes, version suffixes, and date
 * stamps are stripped before an exact, case-insensitive lookup.
 *
 * A model that is not in the table has no price: `priceForUsage` returns
 * `undefined` rather than guessing from a family name, because a wrong rate
 * is worse than a missing one (Opus alone spans $5 and $15 input). Callers
 * that prefer an estimate pass `fallbackRate` explicitly.
 */

export interface BedrockRate {
  /** Canonical model name, the pricing key in the Rust cost engine. */
  model: string;
  input: number;
  output: number;
}

export type BedrockTokenRate = Omit<BedrockRate, "model">;

export const BEDROCK_RATES: readonly BedrockRate[] = [
  { model: "claude-opus-4-8", input: 5, output: 25 },
  { model: "claude-opus-4-7", input: 5, output: 25 },
  { model: "claude-opus-4-6", input: 5, output: 25 },
  { model: "claude-opus-4-5", input: 5, output: 25 },
  { model: "claude-opus-4-1", input: 15, output: 75 },
  { model: "claude-sonnet-4-6", input: 3, output: 15 },
  { model: "claude-sonnet-4-5", input: 3, output: 15 },
  { model: "claude-haiku-4-5", input: 1, output: 5 },
  { model: "claude-haiku-3-5", input: 0.8, output: 4 },
  { model: "claude-3-5-sonnet", input: 3, output: 15 },
  { model: "claude-3-opus", input: 15, output: 75 },
  { model: "claude-3-sonnet", input: 3, output: 15 },
  { model: "claude-3-haiku", input: 0.25, output: 1.25 },
  { model: "nova-premier", input: 2.5, output: 12.5 },
  { model: "nova-pro", input: 0.8, output: 3.2 },
  { model: "nova-lite", input: 0.06, output: 0.24 },
  { model: "nova-micro", input: 0.035, output: 0.14 },
  { model: "titan-embed-text", input: 0.1, output: 0 },
  { model: "cohere-embed", input: 0.1, output: 0 },
];

/**
 * Bedrock model ids whose normalized form differs from the engine's name for
 * the same model. An alias carries no price of its own.
 */
export const BEDROCK_MODEL_ALIASES: Readonly<Record<string, string>> = {
  // anthropic.claude-3-5-haiku-20241022-v1:0
  "claude-3-5-haiku": "claude-haiku-3-5",
  // cohere.embed-english-v3, cohere.embed-multilingual-v3
  "embed-english": "cohere-embed",
  "embed-multilingual": "cohere-embed",
};

/**
 * The Sonnet-class rate that earlier releases applied silently to unknown
 * models. No longer a default: pass it as `fallbackRate` to opt back in.
 */
export const BEDROCK_DEFAULT_RATE: BedrockTokenRate = { input: 3, output: 15 };

export const BEDROCK_RATE_PER_TOKENS = 1_000_000;

const RATE_BY_MODEL: ReadonlyMap<string, BedrockRate> = new Map(BEDROCK_RATES.map((rate) => [rate.model, rate]));

/**
 * Canonical pricing key for a Bedrock model id, inference-profile id, or
 * ARN; mirrors `CostCalculator::normalize_model_id` in the Rust engine.
 */
function pricingKey(model: string): string {
  return model
    .toLowerCase()
    .replace(/^.*\//, "")
    .replace(/^(us|eu|apac|global)\./, "")
    .replace(/^(anthropic|amazon|cohere|mistral|meta)\./, "")
    .replace(/-v[\d:]+$/, "")
    .replace(/:\d+$/, "")
    .replace(/-\d{8}$/, "");
}

/** The list rate for `model`, or `undefined` when the model is not in the table. */
export function rateForModel(model: string): BedrockRate | undefined {
  const exact = model.toLowerCase();
  const key = RATE_BY_MODEL.has(exact) ? exact : pricingKey(model);
  return RATE_BY_MODEL.get(key) ?? RATE_BY_MODEL.get(BEDROCK_MODEL_ALIASES[key] ?? "");
}

/**
 * Prices one call in USD at the model's list rate. Returns `undefined` for a
 * model without a known rate, unless `fallbackRate` supplies one to use.
 */
export function priceForUsage(
  model: string,
  inputTokens: number,
  outputTokens: number,
  options: { fallbackRate: BedrockTokenRate },
): number;
export function priceForUsage(
  model: string,
  inputTokens: number,
  outputTokens: number,
  options?: { fallbackRate?: BedrockTokenRate },
): number | undefined;
export function priceForUsage(
  model: string,
  inputTokens: number,
  outputTokens: number,
  options: { fallbackRate?: BedrockTokenRate } = {},
): number | undefined {
  const rate = rateForModel(model) ?? options.fallbackRate;
  if (rate === undefined) return undefined;
  return (inputTokens * rate.input + outputTokens * rate.output) / BEDROCK_RATE_PER_TOKENS;
}
