"""
Semantic conventions for prompt-knowledge validation.

These attributes should be used consistently across all validation operations
to ensure telemetry data is queryable and comparable.
"""

# Validation status
VALIDATION_STATUS = "validation.status"  # "passed" | "failed" | "warning"
VALIDATION_MODE = "validation.mode"  # "strict" | "tolerant" | "warn_only"
VALIDATION_LAYER = "validation.layer"  # "syntax" | "resolution" | "semantic"

# Reference tracking
VALIDATION_REFERENCE_COUNT = "validation.reference.count"
VALIDATION_REFERENCE_EXTRACTED = "validation.reference.extracted"
VALIDATION_REFERENCE_TYPE = "validation.reference.type"

# Error tracking
VALIDATION_ERROR_COUNT = "validation.error.count"
VALIDATION_ERROR_CODE = "validation.error.code"
VALIDATION_ERROR_MESSAGE = "validation.error.message"
VALIDATION_ERROR_REFERENCE = "validation.error.reference"
VALIDATION_ERROR_SEVERITY = "validation.error.severity"

# Resolution details
VALIDATION_RESOLUTION_TIME_MS = "validation.resolution.time_ms"
VALIDATION_LAKEFS_COMMIT = "validation.lakefs.commit"
VALIDATION_LAKEFS_BRANCH = "validation.lakefs.branch"

# Semantic validation
VALIDATION_SEMANTIC_ENABLED = "validation.semantic.enabled"
VALIDATION_SEMANTIC_MODEL = "validation.semantic.model"
VALIDATION_SEMANTIC_CONFIDENCE = "validation.semantic.confidence"

# Remediation
VALIDATION_REMEDIATION_SUGGESTION = "validation.remediation.suggestion"
