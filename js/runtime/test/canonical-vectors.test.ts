import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { canonicalJson, pythonStrFallback, sha256Hex } from "../src/integrity/index.js";

interface Vector {
  name: string;
  profile: "strict" | "payload";
  input: unknown;
  canonical: string;
  canonical_utf8_hex: string;
  sha256: string;
}

// Generated from the Python implementation by
// tests/vectors/canonical_json_vectors.py; the Python suite checks the same file.
const { vectors } = JSON.parse(
  readFileSync(new URL("../../../tests/vectors/canonical_json.json", import.meta.url), "utf8"),
) as { vectors: Vector[] };

function decode(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(decode);
  if (typeof value === "object" && value !== null) {
    const object = value as Record<string, unknown>;
    const keys = Object.keys(object);
    if (keys.length === 1 && keys[0] === "$bigint") return BigInt(object.$bigint as string);
    if (keys.length === 1 && keys[0] === "$date") return new Date(object.$date as string);
    return Object.fromEntries(keys.map((key) => [key, decode(object[key])]));
  }
  return value;
}

describe("cross-language canonical JSON vectors", () => {
  it("covers the agreed subset", () => {
    expect(vectors.length).toBeGreaterThanOrEqual(30);
    expect(new Set(vectors.map((vector) => vector.name)).size).toBe(vectors.length);
  });

  it.each(vectors.map((vector) => [vector.name, vector] as const))("matches Python for %s", (_name, vector) => {
    const bytes = canonicalJson(
      decode(vector.input),
      vector.profile === "payload" ? { fallback: pythonStrFallback } : {},
    );
    expect(Buffer.from(bytes).toString("utf8")).toBe(vector.canonical);
    expect(Buffer.from(bytes).toString("hex")).toBe(vector.canonical_utf8_hex);
    expect(sha256Hex(bytes)).toBe(vector.sha256);
  });
});
