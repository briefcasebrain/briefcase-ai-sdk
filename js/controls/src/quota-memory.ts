/**
 * In-process token-bucket quota store for demos and tests. This is an
 * approximation, NOT an executable spec of any production store: state lives
 * in one process, and refill timing is coarse (refilledAt jumps to now
 * whenever a refill lands, dropping fractional progress). Policy keys: `capacity` (burst) and `refillSecondsPerToken`
 * (sustained rate). Acquire fails open on internal errors so a limiter bug
 * never blocks a real request.
 */

import type { QuotaDecision, QuotaStore } from "./ports.js";

interface BucketState {
  tokens: number;
  refilledAtMs: number;
}

function keyOf(tenantId: string, bucket: string): string {
  return `${tenantId}\u0000${bucket}`;
}

export class MemoryQuotaStore implements QuotaStore {
  private readonly buckets = new Map<string, BucketState>();
  private readonly cooldowns = new Map<string, number>();

  acquire(args: {
    tenantId: string;
    bucket: string;
    policy: Record<string, unknown>;
    ctx?: unknown;
    now?: unknown;
  }): QuotaDecision {
    try {
      const nowMs = typeof args.now === "number" ? args.now : Date.now();
      const capacity = Number(args.policy["capacity"]);
      const refillSeconds = Number(args.policy["refillSecondsPerToken"]);
      if (!Number.isFinite(capacity) || !Number.isFinite(refillSeconds) || refillSeconds <= 0) {
        throw new Error("policy requires numeric capacity and refillSecondsPerToken");
      }
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
        this.buckets.set(key, { tokens: capacity - 1, refilledAtMs: nowMs });
        return { allowed: true, tokensRemaining: capacity - 1 };
      }

      const elapsedTokens = Math.floor((nowMs - state.refilledAtMs) / 1000 / refillSeconds);
      const refilled = Math.min(capacity, state.tokens + Math.max(0, elapsedTokens));
      if (refilled > state.tokens) state.refilledAtMs = nowMs;
      if (refilled >= 1) {
        state.tokens = refilled - 1;
        return { allowed: true, tokensRemaining: state.tokens };
      }
      state.tokens = 0;
      return { allowed: false, tokensRemaining: 0 };
    } catch {
      return { allowed: true, tokensRemaining: null };
    }
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
    this.buckets.set(key, { tokens: 0, refilledAtMs: nowMs });
    this.cooldowns.set(key, nowMs + args.seconds * 1000);
  }

  /** Test hook: drops all buckets and cooldowns. */
  reset(): void {
    this.buckets.clear();
    this.cooldowns.clear();
  }
  /** Returns one token, capped at the policy's capacity. */
  refund(args: { tenantId: string; bucket: string; policy: Record<string, unknown> }): void {
    const capacity = Number(args.policy["capacity"]);
    if (!Number.isFinite(capacity)) return;
    const state = this.buckets.get(keyOf(args.tenantId, args.bucket));
    if (!state) return;
    state.tokens = Math.min(capacity, state.tokens + 1);
  }
}
