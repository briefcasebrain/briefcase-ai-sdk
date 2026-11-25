import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import {
  ChainConflictError,
  GENESIS_PRIOR_HASH,
  HashChainAppender,
  canonicalJson,
  computeEntryHash,
  computePayloadHash,
  pythonStrFallback,
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

  it("throws instead of hashing non-plain objects as {}", () => {
    const rejected: Array<[string, unknown]> = [
      ["Date", new Date("2026-01-01T00:00:00Z")],
      ["Map", new Map([["a", 1]])],
      ["Set", new Set([1])],
      ["Uint8Array", new Uint8Array([1, 2])],
      ["ArrayBuffer", new ArrayBuffer(2)],
      ["RegExp", /x/],
      ["Error", new Error("boom")],
      ["function", () => 1],
      ["symbol", Symbol("s")],
    ];
    for (const [name, value] of rejected) {
      expect(() => canonicalJson({ value }), name).toThrow(TypeError);
      expect(() => canonicalJson({ value }), name).toThrow(new RegExp(`unsupported canonical JSON value: ${name}`));
    }
    for (const [name, value] of rejected.filter(([name]) => name !== "Date")) {
      expect(() => computePayloadHash({ value }), name).toThrow(TypeError);
    }
    expect(() => canonicalJson([, 1])).toThrow(/unsupported/);
    const circular: Record<string, unknown> = {};
    circular.self = circular;
    expect(() => canonicalJson(circular)).toThrow(/circular/);
    expect(() => canonicalJson({ value: "\ud800" })).toThrow(/well-formed/);
    expect(() => canonicalJson({ "\udc00": 1 })).toThrow(/well-formed/);
  });

  it("covers a Date in a payload hash the way Python str() renders a UTC datetime", () => {
    const text = (value: unknown) => Buffer.from(canonicalJson(value, { fallback: pythonStrFallback })).toString("utf8");
    expect(text({ at: new Date("2026-01-01T00:00:01.001Z") })).toBe('{"at":"2026-01-01 00:00:01.001000+00:00"}');
    expect(text({ at: new Date("2026-01-01T00:00:00Z") })).toBe('{"at":"2026-01-01 00:00:00+00:00"}');
    expect(computePayloadHash({ at: new Date(1) })).not.toBe(computePayloadHash({ at: new Date(2) }));
    expect(computePayloadHash({ at: new Date(1) })).not.toBe(computePayloadHash({ at: {} }));
    expect(() => computePayloadHash({ at: new Date(Number.NaN) })).toThrow(/valid/);
    expect(() => computePayloadHash({ at: new Date("+010000-01-01T00:00:00Z") })).toThrow(/0001-9999/);
  });

  it("encodes bigint as an integer and keeps plain-number output unchanged", () => {
    const text = (value: unknown) => Buffer.from(canonicalJson(value)).toString("utf8");
    expect(text({ n: 9007199254740993n })).toBe('{"n":9007199254740993}');
    expect(text({ a: 1.0, b: 1e16, c: 1.5, d: -0 })).toBe('{"a":1,"b":10000000000000000,"c":1.5,"d":0}');
  });

  it("orders keys by code point, not UTF-16 code unit", () => {
    const text = (value: unknown) => Buffer.from(canonicalJson(value)).toString("utf8");
    expect(text({ "\u{10000}": 1, "\uffff": 2 })).toBe('{"\uffff":2,"\u{10000}":1}');
    expect(text({ b: 1, a: 2, "é": 3, B: 4 })).toBe('{"B":4,"a":2,"b":1,"é":3}');
  });

  it("verifies payload hashes when payloads are supplied", async () => {
    const entries: HashChainEntry[] = [];
    const store: HashChainStore = {
      lastEntryHash: () => entries.at(-1)?.hash ?? GENESIS_PRIOR_HASH,
      append: (entry) => void entries.push(entry),
    };
    const appender = new HashChainAppender(store);
    const payloads = new Map<string, unknown>([["row-1", { n: 1 }], ["row-2", { n: 2 }]]);
    for (const [rowId, payload] of payloads) {
      await appender.appendRow({
        table: "events",
        rowId,
        observedAt: new Date("2026-01-01T00:00:00Z"),
        recordedAt: new Date("2026-01-01T00:00:00Z"),
        payload,
      });
    }
    expect(verifyChainSegment(entries)).toEqual({ ok: true });
    expect(verifyChainSegment(entries, { payloads })).toEqual({ ok: true });
    expect(verifyChainSegment(entries, { payloads: (entry) => payloads.get(entry.rowId) })).toEqual({ ok: true });

    const tampered = new Map(payloads).set("row-2", { n: 3 });
    // Linkage alone cannot see an altered row; the payload check can.
    expect(verifyChainSegment(entries)).toEqual({ ok: true });
    expect(verifyChainSegment(entries, { payloads: tampered })).toEqual({ ok: false, rowId: "row-2", reason: "payload_hash" });
    expect(verifyChainSegment(entries, { payloads: new Map([["row-1", { n: 1 }]]) })).toEqual({
      ok: false,
      rowId: "row-2",
      reason: "payload_missing",
    });
    expect(verifyChainSegment(entries, { payloads: new Map<string, unknown>([["row-1", new Map()]]) })).toEqual({
      ok: false,
      rowId: "row-1",
      reason: "payload_hash",
    });
    expect(verifyChainSegment(entries.slice(1), { expectedPrior: entries[0]!.hash, payloads })).toEqual({ ok: true });
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
