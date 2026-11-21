/**
 * In-process token-bucket quota store for demos and tests. This is an
 * approximation, NOT an executable spec of any production store: state lives
 * in one process, and refill timing is coarse (refilledAt jumps to now
 * whenever a refill lands, dropping fractional progress). Policy keys:
 * `capacity` (burst, >= 1) and `refillSecondsPerToken` (sustained rate, > 0).
 *
 * An invalid policy fails CLOSED: acquire denies rather than letting an
 * unlimited number of calls through a misconfigured bucket. It denies instead
 * of throwing because the gateway's default error policy treats a throwing
 * store as an outage and allows the call.
 *
 * Idle state is evicted: a bucket that has refilled to capacity is
 * indistinguishable from one never seen, so it and any lapsed cooldown are
 * dropped during a periodic sweep (and by `evictExpired`).
 */

import type { QuotaDecision, QuotaStore } from "./ports.js";

interface BucketState {
  tokens: number;
  refilledAtMs: number;
  /** When the bucket is back at capacity and can be forgotten. */
  fullAtMs: number;
  /** Time to refill from empty under the last policy seen; null before any acquire. */
  fullSpanMs: number | null;
}

/** Recomputes when `state` will have refilled to capacity. */
function settle(state: BucketState, capacity: number, refillSeconds: number): void {
  state.fullSpanMs = capacity * refillSeconds * 1000;
  state.fullAtMs = state.refilledAtMs + (capacity - state.tokens) * refillSeconds * 1000;
}

function keyOf(tenantId: string, bucket: string): string {
  return `${tenantId}\u0000${bucket}`;
}

function parsePolicy(policy: Record<string, unknown>): { capacity: number; refillSeconds: number } | null {
  const capacity = Number(policy["capacity"]);
  const refillSeconds = Number(policy["refillSecondsPerToken"]);
  if (!Number.isFinite(capacity) || capacity < 1) return null;
  if (!Number.isFinite(refillSeconds) || refillSeconds <= 0) return null;
  return { capacity, refillSeconds };
}

export interface MemoryQuotaStoreOptions {
  /** Minimum time between automatic eviction sweeps (default 60 000 ms). */
  sweepIntervalMs?: number;
}

export class MemoryQuotaStore implements QuotaStore {
  private readonly buckets = new Map<string, BucketState>();
  private readonly cooldowns = new Map<string, number>();
  private readonly sweepIntervalMs: number;
  private lastSweepMs: number | null = null;

  constructor(options: MemoryQuotaStoreOptions = {}) {
    this.sweepIntervalMs = options.sweepIntervalMs ?? 60_000;
  }

  /** Number of buckets currently held (cooldowns excluded). */
  get size(): number {
    return this.buckets.size;
  }

  acquire(args: {
    tenantId: string;
    bucket: string;
    policy: Record<string, unknown>;
    ctx?: unknown;
    now?: unknown;
  }): QuotaDecision {
    const nowMs = typeof args.now === "number" ? args.now : Date.now();
    const policy = parsePolicy(args.policy);
    if (policy === null) return { allowed: false, tokensRemaining: 0 };
    const { capacity, refillSeconds } = policy;
    this.maybeSweep(nowMs);
    const key = keyOf(args.tenantId, args.bucket);

    const cooldownUntil = this.cooldowns.get(key);
    if (cooldownUntil !== undefined) {
      if (nowMs < cooldownUntil) {
        return { allowed: false, tokensRemaining: 0, cooldownUntil: new Date(cooldownUntil) };
      }
      this.cooldowns.delete(key);
    }

    const state = this.buckets.get(key);
    if (state === undefined) {
      const fresh: BucketState = { tokens: capacity - 1, refilledAtMs: nowMs, fullAtMs: 0, fullSpanMs: null };
      settle(fresh, capacity, refillSeconds);
      this.buckets.set(key, fresh);
      return { allowed: true, tokensRemaining: capacity - 1 };
    }

    const elapsedTokens = Math.floor((nowMs - state.refilledAtMs) / 1000 / refillSeconds);
    const refilled = Math.min(capacity, state.tokens + Math.max(0, elapsedTokens));
    if (refilled > state.tokens) state.refilledAtMs = nowMs;
    if (refilled >= 1) {
      state.tokens = refilled - 1;
      settle(state, capacity, refillSeconds);
      return { allowed: true, tokensRemaining: state.tokens };
    }
    state.tokens = 0;
    settle(state, capacity, refillSeconds);
    return { allowed: false, tokensRemaining: 0 };
  }

  /**
   * Drops buckets that have refilled to capacity and cooldowns that have
   * lapsed. Runs automatically from `acquire`; returns the number of entries
   * removed.
   */
  evictExpired(now: number = Date.now()): number {
    let evicted = 0;
    for (const [key, until] of this.cooldowns) {
      if (now >= until) {
        this.cooldowns.delete(key);
        evicted += 1;
      }
    }
    for (const [key, state] of this.buckets) {
      // A bucket under cooldown stays: it carries the post-cooldown zero balance.
      if (now > state.fullAtMs && !this.cooldowns.has(key)) {
        this.buckets.delete(key);
        evicted += 1;
      }
    }
    this.lastSweepMs = now;
    return evicted;
  }

  private maybeSweep(nowMs: number): void {
    if (this.lastSweepMs === null) {
      this.lastSweepMs = nowMs;
      return;
    }
    // A clock that moved backwards restarts the interval instead of stalling it.
    if (nowMs < this.lastSweepMs) this.lastSweepMs = nowMs;
    else if (nowMs - this.lastSweepMs >= this.sweepIntervalMs) this.evictExpired(nowMs);
  }

  markCooldown(args: {
    tenantId: string;
    bucket: string;
    seconds: number;
    ctx?: unknown;
    now?: unknown;
  }): void {
    const nowMs = typeof args.now === "number" ? args.now : Date.now();
    const key = keyOf(args.tenantId, args.bucket);
    // The zeroed bucket refills from now. Its policy is not passed here, so
    // the refill span comes from the bucket's last acquire; a bucket never
    // acquired is kept until its first acquire supplies one.
    const fullSpanMs = this.buckets.get(key)?.fullSpanMs ?? null;
    this.buckets.set(key, {
      tokens: 0,
      refilledAtMs: nowMs,
      fullAtMs: fullSpanMs === null ? Number.POSITIVE_INFINITY : nowMs + fullSpanMs,
      fullSpanMs,
    });
    this.cooldowns.set(key, nowMs + args.seconds * 1000);
  }

  /** Test hook: drops all buckets and cooldowns. */
  reset(): void {
    this.buckets.clear();
    this.cooldowns.clear();
    this.lastSweepMs = null;
  }

  /** Returns one token, capped at the policy's capacity. */
  refund(args: { tenantId: string; bucket: string; policy: Record<string, unknown> }): void {
    const policy = parsePolicy(args.policy);
    if (policy === null) return;
    const state = this.buckets.get(keyOf(args.tenantId, args.bucket));
    if (!state) return;
    state.tokens = Math.min(policy.capacity, state.tokens + 1);
    settle(state, policy.capacity, policy.refillSeconds);
  }
}
