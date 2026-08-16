/**
 * Six-step suggestion pipeline: cache read, feature flag, entitlements hard
 * cap, quota acquire, provider call, deterministic fallback. The step order
 * is contractual so applications can rely on what runs (and spends) before
 * what. A stored template_fallback row is
 * never served as a cache hit, and when a fingerprint is configured the
 * cached row must carry the same fingerprint. A null fallback fails closed:
 * a terminal failed:<reason> row is written and the result carries
 * source "failed".
 */

import { createHash } from "node:crypto";

import type { CacheStore, EntitlementsHook, QuotaStore, UsageSink } from "./ports.js";
import { classifyProviderError } from "./throttle.js";

export type FallbackReason =
  | "flag_off"
  | "quota_exhausted"
  | "credits_exhausted"
  | "bedrock_failed";

export type SuggestionSource = "cache" | "bedrock" | "template_fallback" | "failed";

/**
 * What the provider closure returns: the generated items plus the token
 * usage and resolved model id. `usage: null` means the provider reported no
 * counts; the call still succeeds, it just is not metered.
 */
export interface BedrockCallResult<T> {
  items: T[];
  usage: { inputTokens: number; outputTokens: number; model: string } | null;
}

export interface SuggestionPipelineResult<T> {
  items: T[];
  /** True when this batch was served from a cached row. */
  cached: boolean;
  source: SuggestionSource;
  /** Set only when source is "template_fallback": why the AI path was skipped. */
  fallbackReason?: FallbackReason;
  /** Set only when source is "failed": the pipeline refused to invent a template. */
  failureCode?: FallbackReason;
}

export interface SuggestionPipelineConfig<T> {
  tenantId: string;
  scopeId: string;
  /** Cache row kind; distinguishes pipelines sharing one store. */
  kind: string;
  /** Cache lifetime in hours, for both read and write. */
  ttlHours: number;
  bucket: string;
  /** Policy passed through to the quota store's acquire. */
  policy?: Record<string, unknown>;
  cache: CacheStore<T>;
  quotaStore: QuotaStore;
  entitlements?: EntitlementsHook;
  usageSink?: UsageSink;
  /** Feature flag; false routes to buildFallback("flag_off"). */
  isEnabled: () => boolean | Promise<boolean>;
  /**
   * Optional source fingerprint. When set, a cached row only counts as a hit
   * if its stored fingerprint matches, so a different source bypasses the
   * cache and regenerates. Written rows carry it; the key is dropped when
   * undefined.
   */
  cacheFingerprint?: string;
  actorId?: string;
  /** App-side row-id generator for cache writes. */
  rowId?: () => string;
  /**
   * Awaits the usage-metering write instead of fire-and-forgetting it.
   * Off-request workers set this so a frozen event loop cannot drop the
   * unsettled capture promise.
   */
  awaitUsageCapture?: boolean;
  /** Cooldown opened on the quota bucket when the provider throttles. */
  cooldownSeconds?: number;
  /** Clock for cache TTL reads. */
  clock?: () => Date;
  ctx?: unknown;
  /** Provider closure invoked when the pipeline reaches the call branch. */
  call: () => Promise<BedrockCallResult<T>>;
  /**
   * Fallback closure. Returns template items, or null to fail closed (write
   * a terminal failed:<reason> row and surface source "failed").
   */
  buildFallback: (reason: FallbackReason) => T[] | null;
}

export function sha256Fingerprint(text: string): string {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

async function writeCacheRow<T>(
  config: SuggestionPipelineConfig<T>,
  items: T[],
  source: string,
): Promise<void> {
  await config.cache.write({
    tenantId: config.tenantId,
    scopeId: config.scopeId,
    kind: config.kind,
    items,
    source,
    ...(config.cacheFingerprint !== undefined ? { fingerprint: config.cacheFingerprint } : {}),
    ...(config.actorId !== undefined ? { actorId: config.actorId } : {}),
    ...(config.rowId !== undefined ? { rowId: config.rowId() } : {}),
    ctx: config.ctx,
  });
}

async function writeAndReturnFallback<T>(
  config: SuggestionPipelineConfig<T>,
  reason: FallbackReason,
): Promise<SuggestionPipelineResult<T>> {
  const items = config.buildFallback(reason);
  if (items === null) {
    await writeCacheRow(config, [], `failed:${reason}`);
    return { items: [], cached: false, source: "failed", failureCode: reason };
  }
  await writeCacheRow(config, items, `template_fallback:${reason}`);
  return { items, cached: false, source: "template_fallback", fallbackReason: reason };
}

export async function runSuggestionPipeline<T>(
  config: SuggestionPipelineConfig<T>,
): Promise<SuggestionPipelineResult<T>> {
  const clock = config.clock ?? (() => new Date());

  const cached = await config.cache.read({
    tenantId: config.tenantId,
    scopeId: config.scopeId,
    kind: config.kind,
    ttlHours: config.ttlHours,
    ...(config.cacheFingerprint !== undefined ? { fingerprint: config.cacheFingerprint } : {}),
    now: clock(),
    ctx: config.ctx,
  });
  if (cached !== null && cached !== undefined) {
    const fingerprintOk =
      config.cacheFingerprint === undefined || cached.fingerprint === config.cacheFingerprint;
    if (
      cached.items.length > 0 &&
      !cached.source.startsWith("template_fallback") &&
      fingerprintOk
    ) {
      return { items: cached.items, cached: true, source: "cache" };
    }
  }

  if (!(await config.isEnabled())) {
    return writeAndReturnFallback(config, "flag_off");
  }

  // Billing stop, checked before the quota bucket: a hard-capped tenant's
  // call never runs, so it must not debit a throttle token either.
  if (config.entitlements !== undefined) {
    if (await config.entitlements.isHardCapped({ tenantId: config.tenantId, ctx: config.ctx })) {
      return writeAndReturnFallback(config, "credits_exhausted");
    }
  }

  const decision = await config.quotaStore.acquire({
    tenantId: config.tenantId,
    bucket: config.bucket,
    policy: config.policy ?? {},
    ctx: config.ctx,
  });
  if (!decision.allowed) {
    return writeAndReturnFallback(config, "quota_exhausted");
  }

  try {
    const { items, usage } = await config.call();
    if (usage !== null && config.usageSink !== undefined) {
      const capture = config.usageSink.capture({
        tenantId: config.tenantId,
        bucket: config.bucket,
        model: usage.model,
        inputTokens: usage.inputTokens,
        outputTokens: usage.outputTokens,
        scopeId: config.scopeId,
        ctx: config.ctx,
      });
      // The UsageSink contract says capture never rejects; the guard only
      // keeps a broken sink from surfacing as an unhandled rejection.
      if (config.awaitUsageCapture === true) await capture;
      else void Promise.resolve(capture).catch(() => {});
    }
    await writeCacheRow(config, items, "bedrock");
    return { items, cached: false, source: "bedrock" };
  } catch (err) {
    if (classifyProviderError(err).throttled) {
      // Deliberately unguarded, unlike the gateway's markCooldown, which
      // swallows cooldown-store failures. This path keeps the legacy
      // pipeline contract: a cooldown-store failure surfaces to the caller.
      await config.quotaStore.markCooldown({
        tenantId: config.tenantId,
        bucket: config.bucket,
        seconds: config.cooldownSeconds ?? 3600,
        ctx: config.ctx,
      });
    }
    return writeAndReturnFallback(config, "bedrock_failed");
  }
}
