import { describe, expect, it } from "vitest";

import { MemoryQuotaStore } from "../src/quota-memory.js";

const POLICY = { capacity: 3, refillSecondsPerToken: 60 };
const T0 = 1_700_000_000_000;

function drain(store: MemoryQuotaStore, now: number, times: number): void {
  for (let i = 0; i < times; i++) {
    store.acquire({ tenantId: "t1", bucket: "draft", policy: POLICY, now });
  }
}

describe("MemoryQuotaStore", () => {
  it("debits one token per acquire down to zero", () => {
    const store = new MemoryQuotaStore();
    for (let i = 0; i < 3; i++) {
      const d = store.acquire({ tenantId: "t1", bucket: "draft", policy: POLICY, now: T0 });
      expect(d.allowed).toBe(true);
      expect(d.tokensRemaining).toBe(3 - (i + 1));
    }
    const denied = store.acquire({ tenantId: "t1", bucket: "draft", policy: POLICY, now: T0 });
    expect(denied.allowed).toBe(false);
    expect(denied.tokensRemaining).toBe(0);
  });

  it("refills by elapsed time up to capacity", () => {
    const store = new MemoryQuotaStore();
    drain(store, T0, 3);
    const still = store.acquire({ tenantId: "t1", bucket: "draft", policy: POLICY, now: T0 + 59_000 });
    expect(still.allowed).toBe(false);
    const after = store.acquire({ tenantId: "t1", bucket: "draft", policy: POLICY, now: T0 + 121_000 });
    expect(after.allowed).toBe(true);
    expect(after.tokensRemaining).toBe(1);
  });

  it("caps refill at capacity", () => {
    const store = new MemoryQuotaStore();
    drain(store, T0, 3);
    const d = store.acquire({
      tenantId: "t1",
      bucket: "draft",
      policy: POLICY,
      now: T0 + 100 * 60_000,
    });
    expect(d.allowed).toBe(true);
    expect(d.tokensRemaining).toBe(2);
  });

  it("isolates tenants and buckets", () => {
    const store = new MemoryQuotaStore();
    drain(store, T0, 3);
    expect(
      store.acquire({ tenantId: "t2", bucket: "draft", policy: POLICY, now: T0 }).allowed,
    ).toBe(true);
    expect(
      store.acquire({ tenantId: "t1", bucket: "polish", policy: POLICY, now: T0 }).allowed,
    ).toBe(true);
  });

  it("denies during cooldown and recovers after refill", () => {
    const store = new MemoryQuotaStore();
    store.markCooldown({ tenantId: "t1", bucket: "draft", seconds: 30, now: T0 });
    const during = store.acquire({ tenantId: "t1", bucket: "draft", policy: POLICY, now: T0 + 10_000 });
    expect(during.allowed).toBe(false);
    expect(during.cooldownUntil).toEqual(new Date(T0 + 30_000));
    // Cooldown zeroes the bucket; one refill interval must elapse before a token exists.
    const after = store.acquire({ tenantId: "t1", bucket: "draft", policy: POLICY, now: T0 + 70_000 });
    expect(after.allowed).toBe(true);
    expect(after.tokensRemaining).toBe(0);
  });

  it("fails closed on a broken policy", () => {
    const store = new MemoryQuotaStore();
    const broken: Array<Record<string, unknown>> = [
      {},
      { capacity: 3 },
      { refillSecondsPerToken: 60 },
      { capacity: "many", refillSecondsPerToken: 60 },
      { capacity: 3, refillSecondsPerToken: 0 },
      { capacity: 3, refillSecondsPerToken: -1 },
      { capacity: 0, refillSecondsPerToken: 60 },
      { capacity: -5, refillSecondsPerToken: 60 },
      { capacity: Number.POSITIVE_INFINITY, refillSecondsPerToken: 60 },
      { capacity: null, refillSecondsPerToken: null },
    ];
    for (const policy of broken) {
      // Repeated calls stay denied: no unlimited pass-through.
      for (let i = 0; i < 3; i++) {
        const d = store.acquire({ tenantId: "t1", bucket: "draft", policy, now: T0 });
        expect(d, JSON.stringify(policy)).toEqual({ allowed: false, tokensRemaining: 0 });
      }
    }
    expect(store.size).toBe(0);
    // A valid policy on the same bucket is unaffected.
    expect(store.acquire({ tenantId: "t1", bucket: "draft", policy: POLICY, now: T0 }).allowed).toBe(true);
  });

  it("evicts buckets that have refilled to capacity", () => {
    const store = new MemoryQuotaStore({ sweepIntervalMs: 1_000 });
    for (let i = 0; i < 50; i++) {
      store.acquire({ tenantId: `tenant-${i}`, bucket: "draft", policy: POLICY, now: T0 });
    }
    drain(store, T0, 3);
    expect(store.size).toBe(51);
    // One token debited refills in 60s; the drained bucket needs 180s.
    store.acquire({ tenantId: "probe", bucket: "draft", policy: POLICY, now: T0 + 61_000 });
    expect(store.size).toBe(2);
    const partial = store.acquire({ tenantId: "t1", bucket: "draft", policy: POLICY, now: T0 + 61_000 });
    expect(partial).toEqual({ allowed: true, tokensRemaining: 0 });
    expect(store.evictExpired(T0 + 61_000 + 181_000)).toBe(2);
    expect(store.size).toBe(0);
  });

  it("eviction never changes a decision", () => {
    const run = (sweepIntervalMs: number) => {
      const store = new MemoryQuotaStore({ sweepIntervalMs });
      const decisions: unknown[] = [];
      for (const offset of [0, 0, 0, 0, 59_000, 60_000, 61_000, 125_000, 400_000, 400_000, 1_000_000]) {
        decisions.push(store.acquire({ tenantId: "t1", bucket: "draft", policy: POLICY, now: T0 + offset }));
      }
      return decisions;
    };
    expect(run(1)).toEqual(run(Number.POSITIVE_INFINITY));
  });

  it("evicts a lapsed cooldown only once its bucket has refilled", () => {
    const store = new MemoryQuotaStore();
    drain(store, T0, 1);
    store.markCooldown({ tenantId: "t1", bucket: "draft", seconds: 30, now: T0 });
    // Cooldown over, but the zeroed bucket has not refilled: it must persist.
    expect(store.evictExpired(T0 + 31_000)).toBe(1);
    expect(store.size).toBe(1);
    const after = store.acquire({ tenantId: "t1", bucket: "draft", policy: POLICY, now: T0 + 70_000 });
    expect(after).toEqual({ allowed: true, tokensRemaining: 0 });
    store.markCooldown({ tenantId: "t1", bucket: "draft", seconds: 30, now: T0 + 70_000 });
    expect(store.evictExpired(T0 + 70_000 + 181_000)).toBe(2);
    expect(store.size).toBe(0);
  });

  it("reset clears all state", () => {
    const store = new MemoryQuotaStore();
    drain(store, T0, 3);
    store.markCooldown({ tenantId: "t1", bucket: "draft", seconds: 3600, now: T0 });
    store.reset();
    expect(
      store.acquire({ tenantId: "t1", bucket: "draft", policy: POLICY, now: T0 }).allowed,
    ).toBe(true);
  });
});

describe("refund", () => {
  it("returns one token capped at capacity and no-ops on a missing bucket", () => {
    const store = new MemoryQuotaStore();
    const policy = { capacity: 3, refillSecondsPerToken: 60 };
    // Missing bucket: no-op.
    store.refund({ tenantId: "t", bucket: "b", policy });
    const first = store.acquire({ tenantId: "t", bucket: "b", policy, now: 1_000 });
    expect(first).toMatchObject({ allowed: true, tokensRemaining: 2 });
    store.refund({ tenantId: "t", bucket: "b", policy });
    // Refund restored the seed debit; clamped at capacity on a second refund.
    store.refund({ tenantId: "t", bucket: "b", policy });
    const next = store.acquire({ tenantId: "t", bucket: "b", policy, now: 1_000 });
    expect(next).toMatchObject({ allowed: true, tokensRemaining: 2 });
  });
});
