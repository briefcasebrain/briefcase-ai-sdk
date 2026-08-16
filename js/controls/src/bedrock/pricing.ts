/**
 * Bedrock list prices, USD per 1M tokens (us-east-1). The table mirrors
 * tests/fixtures/bedrock_rates.json; a test asserts exact parity. Matching
 * is an ordered case-insensitive regex scan, first hit wins, with a default
 * for unknown models.
 */

export interface BedrockRate {
  match: string;
  input: number;
  output: number;
}

export const BEDROCK_RATES: readonly BedrockRate[] = [
  { match: "opus", input: 15, output: 75 },
  { match: "sonnet", input: 3, output: 15 },
  { match: "haiku", input: 0.8, output: 4 },
  { match: "nova-pro", input: 0.8, output: 3.2 },
  { match: "nova-lite", input: 0.06, output: 0.24 },
  { match: "nova-micro", input: 0.035, output: 0.14 },
  { match: "(cohere|embed|titan-embed)", input: 0.1, output: 0 },
];

export const BEDROCK_DEFAULT_RATE: Omit<BedrockRate, "match"> = { input: 3, output: 15 };

export const BEDROCK_RATE_PER_TOKENS = 1_000_000;

// Patterns compiled once at module load; priceForUsage scans this table
// instead of constructing a RegExp per entry per call.
const COMPILED_RATES: readonly { re: RegExp; rate: BedrockRate }[] = BEDROCK_RATES.map((r) => ({
  re: new RegExp(r.match, "i"),
  rate: r,
}));

/** Prices one call in USD at list rates for the first matching table entry. */
export function priceForUsage(model: string, inputTokens: number, outputTokens: number): number {
  const rate = COMPILED_RATES.find((c) => c.re.test(model))?.rate ?? BEDROCK_DEFAULT_RATE;
  return (inputTokens * rate.input + outputTokens * rate.output) / BEDROCK_RATE_PER_TOKENS;
}
