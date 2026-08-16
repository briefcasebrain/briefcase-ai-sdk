/**
 * Semantic conventions for the controls layer. Names and values mirror
 * briefcase/semantic_conventions/controls.py exactly; a test asserts parity.
 */

// Gateway invocation
export const CONTROLS_GATEWAY_BUCKET = "briefcase.controls.gateway.bucket";
export const CONTROLS_GATEWAY_TENANT_ID = "briefcase.controls.gateway.tenant_id";
export const CONTROLS_GATEWAY_OUTCOME = "briefcase.controls.gateway.outcome";

// Quota
export const CONTROLS_QUOTA_TOKENS_REMAINING = "briefcase.controls.quota.tokens_remaining";
export const CONTROLS_QUOTA_COOLDOWN_UNTIL = "briefcase.controls.quota.cooldown_until";

// Cache / fallback pipeline
export const CONTROLS_CACHE_SOURCE = "briefcase.controls.cache.source";
export const CONTROLS_FALLBACK_REASON = "briefcase.controls.fallback.reason";

// Throttle classification
export const CONTROLS_THROTTLE_THROTTLED = "briefcase.controls.throttle.throttled";
export const CONTROLS_THROTTLE_TRANSIENT = "briefcase.controls.throttle.transient";
