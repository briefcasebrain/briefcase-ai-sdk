"""
Semantic conventions for the controls layer (gateway, quota, cache, throttle).
"""

# Gateway invocation
CONTROLS_GATEWAY_BUCKET = "briefcase.controls.gateway.bucket"
CONTROLS_GATEWAY_TENANT_ID = "briefcase.controls.gateway.tenant_id"
CONTROLS_GATEWAY_OUTCOME = "briefcase.controls.gateway.outcome"

# Quota
CONTROLS_QUOTA_TOKENS_REMAINING = "briefcase.controls.quota.tokens_remaining"
CONTROLS_QUOTA_COOLDOWN_UNTIL = "briefcase.controls.quota.cooldown_until"

# Cache / fallback pipeline
CONTROLS_CACHE_SOURCE = "briefcase.controls.cache.source"
CONTROLS_FALLBACK_REASON = "briefcase.controls.fallback.reason"

# Throttle classification
CONTROLS_THROTTLE_THROTTLED = "briefcase.controls.throttle.throttled"
CONTROLS_THROTTLE_TRANSIENT = "briefcase.controls.throttle.transient"
