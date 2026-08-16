/**
 * Retry with exponential backoff for provider calls. Backoff is
 * base * 2^attempt capped, plus bounded jitter. A deadline stops retrying
 * before the next sleep would pass it; an AbortSignal stops retrying between
 * attempts. Only errors the classifier marks transient are retried;
 * everything else rethrows immediately.
 */

import { classifyProviderError, type ThrottleClassification } from "./throttle.js";

export type Classifier = (err: unknown) => ThrottleClassification;

export interface ComputeBackoffOptions {
  baseMs?: number;
  capMs?: number;
  jitterMs?: number;
  rng?: () => number;
}

/** Delay in ms before retry number attempt + 1 (attempt is 0-based). */
export function computeBackoff(attempt: number, options: ComputeBackoffOptions = {}): number {
  const baseMs = options.baseMs ?? 500;
  const capMs = options.capMs ?? 30000;
  const jitterMs = options.jitterMs ?? 500;
  const rng = options.rng ?? Math.random;
  let delay = Math.min(capMs, baseMs * 2 ** attempt);
  if (jitterMs > 0) delay += rng() * jitterMs;
  return delay;
}

export interface WithRetryOptions {
  maxAttempts?: number;
  baseMs?: number;
  capMs?: number;
  jitterMs?: number;
  /** Total budget in ms on `clock`, measured from entry. */
  deadlineMs?: number;
  signal?: AbortSignal;
  classify?: Classifier;
  retryThrottled?: boolean;
  sleep?: (ms: number) => Promise<void>;
  clock?: () => number;
  rng?: () => number;
}

function shouldRetry(err: unknown, classify: Classifier, retryThrottled: boolean): boolean {
  const c = classify(err);
  if (c.throttled) return retryThrottled;
  return c.transient;
}

const defaultSleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Calls `fn` until it succeeds, a non-retryable error throws, attempts run
 * out, the deadline would be passed by the next backoff sleep, or the signal
 * aborts between attempts. Defaults favor short interactive calls; when
 * replacing an existing retry policy, pass explicit options so the backoff
 * schedule stays identical.
 */
export async function withRetry<T>(
  fn: () => T | Promise<T>,
  options: WithRetryOptions = {},
): Promise<T> {
  const maxAttempts = options.maxAttempts ?? 3;
  const classify = options.classify ?? ((err: unknown) => classifyProviderError(err));
  const retryThrottled = options.retryThrottled ?? true;
  const sleep = options.sleep ?? defaultSleep;
  const clock = options.clock ?? Date.now;
  const { deadlineMs, signal } = options;

  const start = clock();
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    try {
      return await fn();
    } catch (err) {
      if (!shouldRetry(err, classify, retryThrottled)) throw err;
      if (attempt + 1 >= maxAttempts) throw err;
      if (signal?.aborted) throw err;
      const delay = computeBackoff(attempt, {
        baseMs: options.baseMs ?? 500,
        capMs: options.capMs ?? 30000,
        jitterMs: options.jitterMs ?? 500,
        rng: options.rng ?? Math.random,
      });
      if (deadlineMs !== undefined && clock() - start + delay > deadlineMs) throw err;
      await sleep(delay);
      if (signal?.aborted) throw err;
    }
  }
  throw new Error("unreachable: retry loop always returns or throws");
}
