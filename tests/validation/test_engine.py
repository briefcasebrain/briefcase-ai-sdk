"""
Tests for the validation engine framework.
"""

import pytest
from unittest.mock import Mock
from briefcase.validation.engine import PromptValidationEngine
from briefcase.validation.errors import ValidationError, ValidationErrorCode


@pytest.fixture
def mock_lakefs():
    """Create mock lakeFS client."""
    lakefs = Mock()
    lakefs.get_commit.return_value = "abc123def456"
    return lakefs


@pytest.fixture
def mock_extractor_empty():
    """Extractor that returns no references."""
    ext = Mock()
    ext.extract.return_value = []
    return ext


@pytest.fixture
def mock_extractor_with_refs():
    """Extractor that returns some references."""
    ext = Mock()
    ext.extract.return_value = ["ref1", "ref2"]
    return ext


@pytest.fixture
def mock_resolver_ok():
    """Resolver that finds no issues."""
    res = Mock()
    res.resolve_all.return_value = []
    return res


@pytest.fixture
def mock_resolver_with_errors():
    """Resolver that returns errors."""
    res = Mock()
    res.resolve_all.return_value = [
        ValidationError(
            code=ValidationErrorCode.REFERENCE_NOT_FOUND,
            message="Not found",
            reference="ref1",
            severity="error",
            layer="resolution",
        )
    ]
    return res


@pytest.fixture
def mock_resolver_with_warnings():
    """Resolver that returns warnings only."""
    res = Mock()
    res.resolve_all.return_value = [
        ValidationError(
            code=ValidationErrorCode.VERSION_MISMATCH,
            message="Version mismatch",
            reference="ref1",
            severity="warning",
            layer="resolution",
        )
    ]
    return res


def test_engine_no_references(mock_lakefs, mock_extractor_empty, mock_resolver_ok):
    engine = PromptValidationEngine(
        extractor=mock_extractor_empty,
        resolver=mock_resolver_ok,
        lakefs_client=mock_lakefs,
        repository="test-kb",
    )
    report = engine.validate("No references here")
    assert report.status == "passed"
    assert report.references_checked == 0
    assert len(report.errors) == 0


def test_engine_valid_references(mock_lakefs, mock_extractor_with_refs, mock_resolver_ok):
    engine = PromptValidationEngine(
        extractor=mock_extractor_with_refs,
        resolver=mock_resolver_ok,
        lakefs_client=mock_lakefs,
        repository="test-kb",
    )
    report = engine.validate("Check ref1 and ref2")
    assert report.status == "passed"
    assert report.references_checked == 2
    assert len(report.errors) == 0


def test_engine_missing_reference(mock_lakefs, mock_extractor_with_refs, mock_resolver_with_errors):
    engine = PromptValidationEngine(
        extractor=mock_extractor_with_refs,
        resolver=mock_resolver_with_errors,
        lakefs_client=mock_lakefs,
        repository="test-kb",
        mode="strict",
    )
    report = engine.validate("Check ref1")
    assert report.status == "failed"
    assert len(report.errors) == 1
    assert report.errors[0].code == ValidationErrorCode.REFERENCE_NOT_FOUND


def test_strict_mode_fails_on_errors(mock_lakefs, mock_extractor_with_refs, mock_resolver_with_errors):
    engine = PromptValidationEngine(
        extractor=mock_extractor_with_refs,
        resolver=mock_resolver_with_errors,
        lakefs_client=mock_lakefs,
        repository="test-kb",
        mode="strict",
    )
    report = engine.validate("test")
    assert report.status == "failed"


def test_strict_mode_warns_on_warnings(mock_lakefs, mock_extractor_with_refs, mock_resolver_with_warnings):
    engine = PromptValidationEngine(
        extractor=mock_extractor_with_refs,
        resolver=mock_resolver_with_warnings,
        lakefs_client=mock_lakefs,
        repository="test-kb",
        mode="strict",
    )
    report = engine.validate("test")
    assert report.status == "warning"


def test_tolerant_mode(mock_lakefs, mock_extractor_with_refs, mock_resolver_with_errors):
    engine = PromptValidationEngine(
        extractor=mock_extractor_with_refs,
        resolver=mock_resolver_with_errors,
        lakefs_client=mock_lakefs,
        repository="test-kb",
        mode="tolerant",
    )
    report = engine.validate("test")
    assert report.status == "failed"


def test_warn_only_mode(mock_lakefs, mock_extractor_with_refs, mock_resolver_with_errors):
    engine = PromptValidationEngine(
        extractor=mock_extractor_with_refs,
        resolver=mock_resolver_with_errors,
        lakefs_client=mock_lakefs,
        repository="test-kb",
        mode="warn_only",
    )
    report = engine.validate("test")
    assert report.status == "passed"


def test_unknown_mode_rejected(mock_lakefs, mock_extractor_with_refs, mock_resolver_with_errors):
    with pytest.raises(ValueError, match="unknown validation mode"):
        PromptValidationEngine(
            extractor=mock_extractor_with_refs,
            resolver=mock_resolver_with_errors,
            lakefs_client=mock_lakefs,
            repository="test-kb",
            mode="Strict",
        )


def test_semantic_layer_called(mock_lakefs, mock_extractor_with_refs, mock_resolver_ok):
    mock_semantic = Mock()
    mock_semantic.validate_semantic.return_value = []

    engine = PromptValidationEngine(
        extractor=mock_extractor_with_refs,
        resolver=mock_resolver_ok,
        lakefs_client=mock_lakefs,
        repository="test-kb",
        semantic_validator=mock_semantic,
    )
    engine.validate("test")
    mock_semantic.validate_semantic.assert_called_once()


def test_semantic_skipped_on_errors(mock_lakefs, mock_extractor_with_refs, mock_resolver_with_errors):
    mock_semantic = Mock()

    engine = PromptValidationEngine(
        extractor=mock_extractor_with_refs,
        resolver=mock_resolver_with_errors,
        lakefs_client=mock_lakefs,
        repository="test-kb",
        semantic_validator=mock_semantic,
    )
    engine.validate("test")
    mock_semantic.validate_semantic.assert_not_called()


def test_report_structure(mock_lakefs, mock_extractor_with_refs, mock_resolver_with_errors):
    engine = PromptValidationEngine(
        extractor=mock_extractor_with_refs,
        resolver=mock_resolver_with_errors,
        lakefs_client=mock_lakefs,
        repository="test-kb",
    )
    report = engine.validate("test")

    assert hasattr(report, "status")
    assert hasattr(report, "errors")
    assert hasattr(report, "warnings")
    assert hasattr(report, "references_checked")
    assert hasattr(report, "validation_time_ms")
    assert hasattr(report, "lakefs_commit")

    d = report.to_dict()
    assert "status" in d
    assert "errors" in d


def test_lakefs_commit_captured(mock_lakefs, mock_extractor_empty, mock_resolver_ok):
    engine = PromptValidationEngine(
        extractor=mock_extractor_empty,
        resolver=mock_resolver_ok,
        lakefs_client=mock_lakefs,
        repository="test-kb",
    )
    report = engine.validate("test")
    assert report.lakefs_commit == "abc123def456"


def test_timing_captured(mock_lakefs, mock_extractor_empty, mock_resolver_ok):
    engine = PromptValidationEngine(
        extractor=mock_extractor_empty,
        resolver=mock_resolver_ok,
        lakefs_client=mock_lakefs,
        repository="test-kb",
    )
    report = engine.validate("test")
    assert report.validation_time_ms >= 0
    assert isinstance(report.validation_time_ms, float)


def test_report_properties(mock_lakefs, mock_extractor_with_refs, mock_resolver_with_errors):
    engine = PromptValidationEngine(
        extractor=mock_extractor_with_refs,
        resolver=mock_resolver_with_errors,
        lakefs_client=mock_lakefs,
        repository="test-kb",
    )
    report = engine.validate("test")
    assert report.has_errors is True
    assert isinstance(report.errors, list)


class _VersionedClientStub:
    """Matches VersionedClient.get_commit(), which takes no arguments."""

    def __init__(self, sha):
        self._sha = sha

    def get_commit(self) -> str:
        return self._sha


def test_engine_reads_commit_from_versioned_client(
    mock_extractor_empty, mock_resolver_ok
):
    engine = PromptValidationEngine(
        extractor=mock_extractor_empty,
        resolver=mock_resolver_ok,
        lakefs_client=_VersionedClientStub("deadbeef"),
        repository="test-kb",
    )
    assert engine.validate("no refs here").lakefs_commit == "deadbeef"


class _TwoArgClientStub:
    """The older contract: get_commit(repository, branch)."""

    def __init__(self, sha):
        self._sha = sha
        self.calls = []

    def get_commit(self, repository: str, branch: str) -> str:
        self.calls.append((repository, branch))
        return self._sha


class _RaisingClientStub:
    def get_commit(self):
        raise RuntimeError("lakeFS unreachable")


def test_engine_reads_commit_from_two_arg_client(
    mock_extractor_empty, mock_resolver_ok
):
    client = _TwoArgClientStub("cafe1234")
    engine = PromptValidationEngine(
        extractor=mock_extractor_empty,
        resolver=mock_resolver_ok,
        lakefs_client=client,
        repository="test-kb",
        branch="release",
    )
    assert engine.validate("no refs here").lakefs_commit == "cafe1234"
    assert client.calls == [("test-kb", "release")]


def test_engine_commit_is_unknown_when_lookup_fails(
    mock_extractor_empty, mock_resolver_ok
):
    engine = PromptValidationEngine(
        extractor=mock_extractor_empty,
        resolver=mock_resolver_ok,
        lakefs_client=_RaisingClientStub(),
        repository="test-kb",
    )
    assert engine.validate("no refs here").lakefs_commit == "unknown"


def test_engine_reads_commit_from_versioned_client_with_references(
    mock_extractor_with_refs, mock_resolver_ok
):
    engine = PromptValidationEngine(
        extractor=mock_extractor_with_refs,
        resolver=mock_resolver_ok,
        lakefs_client=_VersionedClientStub("deadbeef"),
        repository="test-kb",
    )
    assert engine.validate("see {{doc}}").lakefs_commit == "deadbeef"
