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

  it("fails open on a broken policy", () => {
    const store = new MemoryQuotaStore();
    const d = store.acquire({ tenantId: "t1", bucket: "draft", policy: {}, now: T0 });
    expect(d.allowed).toBe(true);
    expect(d.tokensRemaining).toBeNull();
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
