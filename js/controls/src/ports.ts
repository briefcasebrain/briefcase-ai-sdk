/**
 * Ports for @briefcase-ai/controls. The application implements these over its
 * own storage and policy systems. Port methods may return values directly or
 * as promises; callers await either. Every method accepts an opaque `ctx` so
 * an adapter can thread a transaction or request context through without the
 * SDK knowing its type. Store policies travel as plain objects interpreted by
 * the concrete store.
 */

/** How a failing port call is handled: rethrow, treat as allowed, or treat as denied. */
export type PortErrorPolicy = "propagate" | "allow" | "deny";

export const PORT_ERROR_POLICIES: readonly PortErrorPolicy[] = ["propagate", "allow", "deny"];

/**
 * Result of a quota acquire: whether the call may proceed, tokens left after
 * the decision, and any active cooldown deadline.
 */
export interface QuotaDecision {
  allowed: boolean;
  tokensRemaining?: number | null;
  cooldownUntil?: Date | null;
}

export interface QuotaStore {
  /** Debits one call from the tenant's bucket. */
  acquire(args: {
    tenantId: string;
    bucket: string;
    policy: Record<string, unknown>;
    ctx?: unknown;
    now?: unknown;
  }): QuotaDecision | Promise<QuotaDecision>;

  /** Opens a cooldown on the bucket so subsequent acquires fail fast. */
  markCooldown(args: {
    tenantId: string;
    bucket: string;
    seconds: number;
    ctx?: unknown;
    now?: unknown;
  }): void | Promise<void>;

  /**
   * Returns one debited unit, capped at the bucket's capacity. Optional:
   * streaming callers refund when the client cancels before the provider
   * finishes; stores without a refund keep up-front debits final.
   */
  refund?(args: {
    tenantId: string;
    bucket: string;
    policy: Record<string, unknown>;
    ctx?: unknown;
    now?: unknown;
  }): void | Promise<void>;
}

export interface EntitlementsHook {
  /** True when the tenant's plan blocks any further AI spend. */
  isHardCapped(args: { tenantId: string; ctx?: unknown }): boolean | Promise<boolean>;
}

/**
 * A cached suggestion row: the entry id in the backing store, the items, the
 * raw source label, and the optional content fingerprint.
 */
export interface CacheEntry<T = unknown> {
  entryId: string | null;
  items: T[];
  source: string;
  fingerprint?: string;
}

/**
 * Suggestion cache port. `read` returns the latest matching row inside the
 * TTL window, or null; latest-row-only selection is the adapter's job. Hit
 * rules (non-empty items, source, fingerprint) live in the caller. `write`
 * appends an entry; row-id generation stays application-side via `rowId`,
 * and adapters drop the fingerprint key when it is undefined.
 */
export interface CacheStore<T = unknown> {
  read(args: {
    tenantId: string;
    scopeId: string;
    kind: string;
    ttlHours: number;
    fingerprint?: string;
    now?: Date;
    ctx?: unknown;
  }): CacheEntry<T> | null | Promise<CacheEntry<T> | null>;

  write(args: {
    tenantId: string;
    scopeId: string;
    kind: string;
    items: T[];
    source: string;
    fingerprint?: string;
    actorId?: string;
    rowId?: string;
    ctx?: unknown;
  }): void | Promise<void>;
}

/**
 * Records token usage for one call. Implementations must never throw or
 * reject; callers do not guard this path.
 */
export interface UsageSink {
  capture(args: {
    tenantId: string;
    bucket: string;
    model: string;
    inputTokens: number;
    outputTokens: number;
    scopeId?: string;
    ctx?: unknown;
  }): void | Promise<void>;
}

/** Decision-record exporter, mirroring the Python BaseExporter surface. */
export interface TraceExporter {
  /** Exports a single decision record. Returns true on success. */
  export(record: unknown, ctx?: unknown): boolean | Promise<boolean>;
  /** Flushes any buffered records. */
  flush(ctx?: unknown): void | Promise<void>;
  /** Cleans up resources. */
  close(ctx?: unknown): void | Promise<void>;
}
