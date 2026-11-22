import { describe, expect, it } from "vitest";

import {
  canonicalBytes,
  canonicalJson,
  canonicalJsonV2,
  computeEntryHash,
  computePayloadHash,
  HASH_SCHEME_V1,
  HASH_SCHEME_V2,
  HashChainAppender,
  type HashChainEntry,
  type HashChainStore,
  verifyChainSegment,
} from "../src/integrity/index.js";

class MemoryStore implements HashChainStore {
  entries: HashChainEntry[] = [];
  lastEntryHash(): string {
    return this.entries.at(-1)?.hash ?? "00".repeat(32);
  }
  append(entry: HashChainEntry): void {
    this.entries.push(entry);
  }
}

const decode = (bytes: Uint8Array) => Buffer.from(bytes).toString("utf8");

describe("jcs-1 (RFC 8785)", () => {
  it("formats numbers like ECMAScript and differs from json-sorted-1 only there", () => {
    expect(decode(canonicalJsonV2({ b: 1e21, a: [1.0, -0, 1e-7, 0.1 + 0.2] }))).toBe(
      '{"a":[1,0,1e-7,0.30000000000000004],"b":1e+21}',
    );
    expect(decode(canonicalBytes({ a: "x" }, HASH_SCHEME_V1))).toBe(decode(canonicalJson({ a: "x" })));
  });

  it("sorts keys by UTF-16 code units, not code points", () => {
    expect(decode(canonicalJsonV2({ "￿": 1, "\u{10000}": 2 }))).toBe('{"\u{10000}":2,"￿":1}');
    expect(decode(canonicalJson({ "￿": 1, "\u{10000}": 2 }))).toBe('{"￿":1,"\u{10000}":2}');
  });

  it("accepts a bigint only when a double holds it exactly", () => {
    expect(decode(canonicalJsonV2(2n ** 60n))).toBe("1152921504606847000");
    expect(() => canonicalJsonV2(2n ** 53n + 1n)).toThrow(RangeError);
    expect(() => canonicalJsonV2(Number.NaN)).toThrow(TypeError);
    expect(() => canonicalJsonV2("\ud800")).toThrow(TypeError);
    expect(() => canonicalBytes({}, "md5-json")).toThrow(/unknown hash scheme/);
  });

  it("chains opt in per appender and verifiers dispatch per entry", async () => {
    const store = new MemoryStore();
    const at = new Date("2026-01-01T00:00:00Z");
    const v1 = await new HashChainAppender(store).appendRow({
      table: "t", rowId: "r1", observedAt: at, recordedAt: at, payload: { x: 1 },
    });
    const v2 = await new HashChainAppender(store, { hashScheme: HASH_SCHEME_V2 }).appendRow({
      table: "t", rowId: "r2", observedAt: at, recordedAt: at, payload: { x: 1.5 },
    });
    expect(v1.hashScheme).toBe(HASH_SCHEME_V1);
    expect(v2.hashScheme).toBe(HASH_SCHEME_V2);
    expect(v2.payloadHash).toBe(computePayloadHash({ x: 1.5 }, HASH_SCHEME_V2));
    const payloads = new Map<string, unknown>([["r1", { x: 1 }], ["r2", { x: 1.5 }]]);
    expect(verifyChainSegment(store.entries, { payloads })).toEqual({ ok: true });
    const relabelled = { ...v2, hashScheme: HASH_SCHEME_V1 };
    expect(computeEntryHash(relabelled)).not.toBe(v2.hash);
    expect(verifyChainSegment([v1, relabelled])).toEqual({ ok: false, rowId: "r2", reason: "hash" });
    expect(verifyChainSegment([v1, { ...v2, hashScheme: "nope" }])).toEqual({
      ok: false, rowId: "r2", reason: "hash_scheme",
    });
  });
});
