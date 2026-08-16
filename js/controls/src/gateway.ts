/**
 * The controls gateway: one enforced path for AI invocations. Composition
 * order: entitlements hard-cap, quota acquire, run the call, and on a
 * throttled failure open a bucket cooldown. The wrapped call only runs after
 * quota is acquired, so no path spends without a debit. Outcomes are typed;
 * port errors follow an explicit policy. When an exporter is configured,
 * each invocation emits a decision record carrying semantic-convention
 * attributes only, never the call's content.
 */

import { randomUUID } from "node:crypto";

import {
  PORT_ERROR_POLICIES,
  type EntitlementsHook,
  type PortErrorPolicy,
  type QuotaDecision,
  type QuotaStore,
  type TraceExporter,
} from "./ports.js";
import { classifyProviderError } from "./throttle.js";
import {
  CONTROLS_GATEWAY_BUCKET,
  CONTROLS_GATEWAY_OUTCOME,
  CONTROLS_GATEWAY_TENANT_ID,
  CONTROLS_QUOTA_COOLDOWN_UNTIL,
  CONTROLS_QUOTA_TOKENS_REMAINING,
} from "./semconv.js";

const DEFAULT_EVENT_NAMES: Record<string, string> = {
  ok: "gateway_ok",
  hard_capped: "gateway_hard_capped",
  quota_exhausted: "gateway_quota_exhausted",
  throttled: "gateway_throttled",
  internal: "gateway_internal_error",
};

export type GatewayFailureReason = "hard_capped" | "quota_exhausted" | "throttled" | "internal";

export type GatewayOutcome<T> =
  | { ok: true; value: T; tokensRemaining?: number | null }
  | {
      ok: false;
      reason: GatewayFailureReason;
      tokensRemaining?: number | null;
      cooldownUntil?: Date | null;
      cause?: unknown;
    };

export interface GatewayConfig {
  quotaStore: QuotaStore;
  buckets: Record<string, Record<string, unknown>>;
  entitlements?: EntitlementsHook;
  cooldownSeconds?: number;
  exporter?: TraceExporter;
  awaitExport?: boolean;
  onOutcome?: (eventName: string, payload: Record<string, unknown>) => void;
  eventNames?: Record<string, string>;
  portErrorPolicy?: PortErrorPolicy;
  /**
   * Enable when migrating from a throttle predicate that always applied
   * message-text matching, so classification stays identical during rollout.
   */
  messageRegexThrottle?: boolean;
  clock?: () => Date;
}

export interface InvokeArgs<T> {
  tenantId: string;
  bucket: string;
  /** Runs only after quota is acquired. Receives an AbortSignal when deadlineMs is set. */
  fn: (signal?: AbortSignal) => T | Promise<T>;
  ctx?: unknown;
  deadlineMs?: number;
}

export interface Gateway {
  invoke<T>(args: InvokeArgs<T>): Promise<GatewayOutcome<T>>;
}

/** Invokes AI calls behind hard-cap, quota, and cooldown enforcement. */
export function createGateway(config: GatewayConfig): Gateway {
  if (config.quotaStore === undefined || config.quotaStore === null) {
    throw new Error("GatewayConfig.quotaStore is required");
  }
  const policy = config.portErrorPolicy ?? "propagate";
  if (!PORT_ERROR_POLICIES.includes(policy)) {
    throw new Error(
      `portErrorPolicy must be one of ${JSON.stringify(PORT_ERROR_POLICIES)}, got ${JSON.stringify(policy)}`,
    );
  }
  const cooldownSeconds = config.cooldownSeconds ?? 3600;
  const clock = config.clock ?? (() => new Date());
  const events = { ...DEFAULT_EVENT_NAMES, ...config.eventNames };

  async function hardCapped(tenantId: string, ctx: unknown): Promise<boolean> {
    if (config.entitlements === undefined) return false;
    try {
      return Boolean(await config.entitlements.isHardCapped({ tenantId, ctx }));
    } catch (err) {
      if (policy === "propagate") throw err;
      return policy === "deny";
    }
  }

  async function acquire(
    tenantId: string,
    bucket: string,
    bucketPolicy: Record<string, unknown>,
    ctx: unknown,
  ): Promise<QuotaDecision> {
    try {
      return await config.quotaStore.acquire({ tenantId, bucket, policy: bucketPolicy, ctx });
    } catch (err) {
      if (policy === "propagate") throw err;
      if (policy === "deny") return { allowed: false, tokensRemaining: 0 };
      return { allowed: true, tokensRemaining: null };
    }
  }

  function emitEvent<T>(outcome: GatewayOutcome<T>, tenantId: string, bucket: string): void {
    if (config.onOutcome === undefined) return;
    const key = outcome.ok ? "ok" : outcome.reason;
    const cooldownUntil = outcome.ok ? null : (outcome.cooldownUntil ?? null);
    const payload: Record<string, unknown> = {
      tenantId,
      bucket,
      outcome: key,
      tokensRemaining: outcome.tokensRemaining ?? null,
      cooldownUntil: cooldownUntil ? cooldownUntil.toISOString() : null,
    };
    try {
      config.onOutcome(events[key] ?? key, payload);
    } catch {
      // Observer failures never affect the invocation.
    }
  }

  async function exportRecord<T>(
    outcome: GatewayOutcome<T>,
    tenantId: string,
    bucket: string,
    fn: (signal?: AbortSignal) => unknown,
    startedAt: Date,
  ): Promise<void> {
    if (config.exporter === undefined) return;
    const endedAt = clock();
    const key = outcome.ok ? "ok" : outcome.reason;
    const cooldownUntil = outcome.ok ? null : (outcome.cooldownUntil ?? null);
    const record: Record<string, unknown> = {
      decision_id: randomUUID(),
      decision_type: `controls.gateway.${bucket}`,
      function_name: fn.name === "" ? "<callable>" : fn.name,
      inputs: {
        [CONTROLS_GATEWAY_TENANT_ID]: tenantId,
        [CONTROLS_GATEWAY_BUCKET]: bucket,
      },
      outputs: {
        [CONTROLS_GATEWAY_OUTCOME]: key,
        [CONTROLS_QUOTA_TOKENS_REMAINING]: outcome.tokensRemaining ?? null,
        [CONTROLS_QUOTA_COOLDOWN_UNTIL]: cooldownUntil ? cooldownUntil.toISOString() : null,
      },
      started_at: startedAt.toISOString(),
      ended_at: endedAt.toISOString(),
      execution_time_ms: endedAt.getTime() - startedAt.getTime(),
    };
    if (!outcome.ok && outcome.cause !== undefined && outcome.cause !== null) {
      record["error"] = causeClassName(outcome.cause);
    }
    const doExport = async () => {
      try {
        await config.exporter!.export(record);
      } catch {
        // Export failures never affect the invocation.
      }
    };
    if (config.awaitExport === true) await doExport();
    else void doExport();
  }

  async function finish<T>(
    outcome: GatewayOutcome<T>,
    tenantId: string,
    bucket: string,
    fn: (signal?: AbortSignal) => unknown,
    startedAt: Date,
  ): Promise<GatewayOutcome<T>> {
    emitEvent(outcome, tenantId, bucket);
    await exportRecord(outcome, tenantId, bucket, fn, startedAt);
    return outcome;
  }

  return {
    async invoke<T>(args: InvokeArgs<T>): Promise<GatewayOutcome<T>> {
      const { tenantId, bucket, fn, ctx, deadlineMs } = args;
      const bucketPolicy = config.buckets[bucket];
      if (bucketPolicy === undefined) {
        throw new Error(
          `Unknown bucket ${JSON.stringify(bucket)}; configured: ${JSON.stringify(Object.keys(config.buckets).sort())}`,
        );
      }
      const startedAt = clock();

      if (await hardCapped(tenantId, ctx)) {
        return finish<T>(
          { ok: false, reason: "hard_capped", tokensRemaining: 0 },
          tenantId,
          bucket,
          fn,
          startedAt,
        );
      }

      const decision = await acquire(tenantId, bucket, bucketPolicy, ctx);
      if (!decision.allowed) {
        return finish<T>(
          {
            ok: false,
            reason: "quota_exhausted",
            tokensRemaining: decision.tokensRemaining ?? null,
            cooldownUntil: decision.cooldownUntil ?? null,
          },
          tenantId,
          bucket,
          fn,
          startedAt,
        );
      }

      let outcome: GatewayOutcome<T>;
      try {
        const signal = deadlineMs === undefined ? undefined : AbortSignal.timeout(deadlineMs);
        const value = await fn(signal);
        outcome = { ok: true, value, tokensRemaining: decision.tokensRemaining ?? null };
      } catch (err) {
        const classification = classifyProviderError(err, {
          messageRegex: config.messageRegexThrottle ?? false,
        });
        if (classification.throttled) {
          try {
            await config.quotaStore.markCooldown({ tenantId, bucket, seconds: cooldownSeconds, ctx });
          } catch {
            // The throttled outcome already reports the failure; a broken
            // cooldown write must not mask it.
          }
          outcome = {
            ok: false,
            reason: "throttled",
            tokensRemaining: 0,
            // Advisory deadline on the gateway clock; the store's own clock
            // governs the enforced cooldown.
            cooldownUntil: new Date(clock().getTime() + cooldownSeconds * 1000),
            cause: err,
          };
        } else {
          outcome = { ok: false, reason: "internal", cause: err };
        }
      }
      return finish(outcome, tenantId, bucket, fn, startedAt);
    },
  };
}

function causeClassName(cause: unknown): string {
  if (typeof cause === "object" && cause !== null) {
    const ctor = (cause as { constructor?: { name?: string } }).constructor;
    return ctor?.name ?? "Object";
  }
  return typeof cause;
}

/** Legacy three-shape outcome surface: throttles and caps collapse into quota_exhausted. */
export type LegacyInvokeOutcome<T> =
  | { ok: true; value: T; tokensRemaining: number }
  | { ok: false; reason: "quota_exhausted"; tokensRemaining: number; cooldownUntil: Date | null }
  | { ok: false; reason: "internal"; cause: unknown };

/**
 * Collapses a typed gateway outcome into the legacy three-shape contract:
 * hard_capped and throttled become quota_exhausted, with the throttle
 * cooldown recomputed as now + cooldownSeconds. Kept for applications
 * migrating from a gateway with that surface; full parity also needs
 * messageRegexThrottle: true when the legacy predicate always applied the
 * message regex.
 */
export function collapseLegacyReason<T>(
  outcome: GatewayOutcome<T>,
  options: { cooldownSeconds?: number; now?: () => number } = {},
): LegacyInvokeOutcome<T> {
  const cooldownSeconds = options.cooldownSeconds ?? 3600;
  const now = options.now ?? Date.now;
  if (outcome.ok) {
    return { ok: true, value: outcome.value, tokensRemaining: outcome.tokensRemaining ?? 0 };
  }
  switch (outcome.reason) {
    case "hard_capped":
      return { ok: false, reason: "quota_exhausted", tokensRemaining: 0, cooldownUntil: null };
    case "quota_exhausted":
      return {
        ok: false,
        reason: "quota_exhausted",
        tokensRemaining: outcome.tokensRemaining ?? 0,
        cooldownUntil: outcome.cooldownUntil ?? null,
      };
    case "throttled":
      return {
        ok: false,
        reason: "quota_exhausted",
        tokensRemaining: 0,
        cooldownUntil: new Date(now() + cooldownSeconds * 1000),
      };
    case "internal":
      return { ok: false, reason: "internal", cause: outcome.cause };
  }
}
