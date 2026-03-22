"""
Tests for validation semantic conventions.
"""

def test_validation_constants_exist():
    """Verify all validation constants are defined."""
    from briefcase.semantic_conventions.validation import (
        VALIDATION_STATUS,
        VALIDATION_MODE,
        VALIDATION_LAYER,
        VALIDATION_REFERENCE_COUNT,
        VALIDATION_REFERENCE_EXTRACTED,
        VALIDATION_REFERENCE_TYPE,
        VALIDATION_ERROR_COUNT,
        VALIDATION_ERROR_CODE,
        VALIDATION_ERROR_MESSAGE,
        VALIDATION_ERROR_REFERENCE,
        VALIDATION_ERROR_SEVERITY,
        VALIDATION_RESOLUTION_TIME_MS,
        VALIDATION_LAKEFS_COMMIT,
        VALIDATION_LAKEFS_BRANCH,
        VALIDATION_SEMANTIC_ENABLED,
        VALIDATION_SEMANTIC_MODEL,
        VALIDATION_SEMANTIC_CONFIDENCE,
        VALIDATION_REMEDIATION_SUGGESTION,
    )

    assert VALIDATION_STATUS == "validation.status"
    assert VALIDATION_MODE == "validation.mode"
    assert VALIDATION_LAYER == "validation.layer"
    assert VALIDATION_REFERENCE_COUNT == "validation.reference.count"
    assert VALIDATION_REFERENCE_EXTRACTED == "validation.reference.extracted"
    assert VALIDATION_REFERENCE_TYPE == "validation.reference.type"
    assert VALIDATION_ERROR_COUNT == "validation.error.count"
    assert VALIDATION_ERROR_CODE == "validation.error.code"
    assert VALIDATION_ERROR_MESSAGE == "validation.error.message"
    assert VALIDATION_ERROR_REFERENCE == "validation.error.reference"
    assert VALIDATION_ERROR_SEVERITY == "validation.error.severity"
    assert VALIDATION_RESOLUTION_TIME_MS == "validation.resolution.time_ms"
    assert VALIDATION_LAKEFS_COMMIT == "validation.lakefs.commit"
    assert VALIDATION_LAKEFS_BRANCH == "validation.lakefs.branch"
    assert VALIDATION_SEMANTIC_ENABLED == "validation.semantic.enabled"
    assert VALIDATION_SEMANTIC_MODEL == "validation.semantic.model"
    assert VALIDATION_SEMANTIC_CONFIDENCE == "validation.semantic.confidence"
    assert VALIDATION_REMEDIATION_SUGGESTION == "validation.remediation.suggestion"


def test_validation_constants_unique():
    """Verify all validation constants have unique values."""
    from briefcase.semantic_conventions import validation

    constants = [
        getattr(validation, attr)
        for attr in dir(validation)
        if attr.startswith('VALIDATION_')
    ]

    # Check for duplicates
    assert len(constants) == len(set(constants)), "Duplicate constant values found"
