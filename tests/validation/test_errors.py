"""
Tests for validation error taxonomy.
"""

from briefcase.validation.errors import (
    ValidationError,
    ValidationErrorCode,
    ValidationReport,
)


def test_validation_error_creation():
    """Test ValidationError dataclass."""
    error = ValidationError(
        code=ValidationErrorCode.REFERENCE_NOT_FOUND,
        message="File not found",
        reference="doc.pdf",
        severity="error",
        layer="resolution",
        remediation="Check file path"
    )

    assert error.code == ValidationErrorCode.REFERENCE_NOT_FOUND
    assert error.message == "File not found"
    assert error.reference == "doc.pdf"
    assert error.severity == "error"
    assert error.layer == "resolution"
    assert error.remediation == "Check file path"


def test_validation_error_to_dict():
    """Test ValidationError to_dict conversion."""
    error = ValidationError(
        code=ValidationErrorCode.REFERENCE_NOT_FOUND,
        message="File not found",
        reference="doc.pdf",
        severity="error",
        layer="resolution",
        remediation="Check file path",
        metadata={'attempted_path': '/docs/doc.pdf'}
    )

    error_dict = error.to_dict()

    assert error_dict['code'] == 404
    assert error_dict['message'] == "File not found"
    assert error_dict['reference'] == "doc.pdf"
    assert error_dict['severity'] == "error"
    assert error_dict['layer'] == "resolution"
    assert error_dict['remediation'] == "Check file path"
    assert error_dict['metadata'] == {'attempted_path': '/docs/doc.pdf'}


def test_validation_error_codes():
    """Test all error codes are properly defined."""
    assert ValidationErrorCode.REFERENCE_NOT_FOUND.value == 404
    assert ValidationErrorCode.REFERENCE_GONE.value == 410
    assert ValidationErrorCode.VERSION_MISMATCH.value == 409
    assert ValidationErrorCode.INVALID_SYNTAX.value == 400
    assert ValidationErrorCode.REFERENCE_AMBIGUOUS.value == 300
    assert ValidationErrorCode.LAKEFS_UNAVAILABLE.value == 503
    assert ValidationErrorCode.SCHEMA_INVALID.value == 500


def test_validation_report_creation():
    """Test ValidationReport dataclass."""
    error = ValidationError(
        ValidationErrorCode.REFERENCE_NOT_FOUND,
        "Not found", "doc.pdf", "error", "resolution"
    )
    warning = ValidationError(
        ValidationErrorCode.VERSION_MISMATCH,
        "Version mismatch", "doc.pdf", "warning", "resolution"
    )

    report = ValidationReport(
        status="failed",
        errors=[error],
        warnings=[warning],
        references_checked=5,
        validation_time_ms=12.5,
        lakefs_commit="abc123"
    )

    assert report.status == "failed"
    assert len(report.errors) == 1
    assert len(report.warnings) == 1
    assert report.references_checked == 5
    assert report.validation_time_ms == 12.5
    assert report.lakefs_commit == "abc123"


def test_validation_report_properties():
    """Test ValidationReport has_errors and has_warnings properties."""
    # Report with errors
    error = ValidationError(
        ValidationErrorCode.REFERENCE_NOT_FOUND,
        "Not found", "doc.pdf", "error", "resolution"
    )
    report_with_errors = ValidationReport(
        status="failed",
        errors=[error],
        warnings=[],
        references_checked=1,
        validation_time_ms=5.0,
        lakefs_commit="abc123"
    )

    assert report_with_errors.has_errors is True
    assert report_with_errors.has_warnings is False

    # Report with warnings
    warning = ValidationError(
        ValidationErrorCode.VERSION_MISMATCH,
        "Version mismatch", "doc.pdf", "warning", "resolution"
    )
    report_with_warnings = ValidationReport(
        status="warning",
        errors=[],
        warnings=[warning],
        references_checked=1,
        validation_time_ms=5.0,
        lakefs_commit="abc123"
    )

    assert report_with_warnings.has_errors is False
    assert report_with_warnings.has_warnings is True

    # Report with neither
    report_passed = ValidationReport(
        status="passed",
        errors=[],
        warnings=[],
        references_checked=0,
        validation_time_ms=1.0,
        lakefs_commit="abc123"
    )

    assert report_passed.has_errors is False
    assert report_passed.has_warnings is False


def test_validation_report_to_dict():
    """Test ValidationReport to_dict conversion."""
    error = ValidationError(
        ValidationErrorCode.REFERENCE_NOT_FOUND,
        "Not found", "doc.pdf", "error", "resolution"
    )
    warning = ValidationError(
        ValidationErrorCode.VERSION_MISMATCH,
        "Version mismatch", "doc.pdf", "warning", "resolution"
    )

    report = ValidationReport(
        status="failed",
        errors=[error],
        warnings=[warning],
        references_checked=5,
        validation_time_ms=12.5,
        lakefs_commit="abc123"
    )

    report_dict = report.to_dict()

    assert report_dict['status'] == "failed"
    assert len(report_dict['errors']) == 1
    assert len(report_dict['warnings']) == 1
    assert report_dict['references_checked'] == 5
    assert report_dict['validation_time_ms'] == 12.5
    assert report_dict['lakefs_commit'] == "abc123"
    assert isinstance(report_dict['errors'][0], dict)
    assert isinstance(report_dict['warnings'][0], dict)
