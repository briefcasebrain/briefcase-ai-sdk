"""Decision-record wire-schema parity against the shared golden fixture.

The TypeScript package asserts the same fixture (js/controls/test/record.test.ts),
so a schema change must touch the fixture and both suites together.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from briefcase.decorators import capture

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "decision_record.json").read_text()
)
OPTIONAL = set(FIXTURE["optional_fields"])
# One required list for both languages: the TypeScript recorder asserts the
# same set, so a field Python always emits must be emitted there too.
REQUIRED = set(FIXTURE["required_fields"])


def _capture_record(**kwargs):
    exp = MagicMock()
    exp.export = MagicMock(return_value=None)

    @capture(exporter=exp, async_capture=False, **kwargs)
    def fn(x):
        return x

    fn("v")
    return exp.export.call_args[0][0]


def test_success_record_has_exactly_the_required_fields():
    record = _capture_record()
    assert set(record) == REQUIRED


def test_context_version_is_the_documented_optional():
    record = _capture_record(context_version="v3")
    assert set(record) == REQUIRED | {"context_version"}
    assert "context_version" in OPTIONAL


def test_error_record_adds_only_the_error_field():
    exp = MagicMock()
    exp.export = MagicMock(return_value=None)

    @capture(exporter=exp, async_capture=False)
    def fn():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        fn()
    record = exp.export.call_args[0][0]
    assert set(record) == REQUIRED | {"error"}
    assert "error" in OPTIONAL


def test_fixture_example_conforms_to_its_own_schema():
    example = FIXTURE["example"]
    shared = set(FIXTURE["required_fields"])
    assert shared.issubset(set(example))
    assert set(example) - shared <= OPTIONAL
    assert not (shared & OPTIONAL)
    assert example["schema_version"] == FIXTURE["schema_version"]


def test_fixture_has_no_per_language_field_lists():
    # A "python_always_fields"-style escape hatch is how the two recorders
    # drifted apart; the fixture carries exactly one required list.
    assert set(FIXTURE) == {
        "description", "schema_version", "required_fields", "optional_fields", "example",
    }
    assert "schema_version" in FIXTURE["required_fields"]


def test_schema_version_is_emitted_and_documented():
    from briefcase.decorators import SCHEMA_VERSION

    record = _capture_record()
    assert record["schema_version"] == SCHEMA_VERSION == FIXTURE["schema_version"] == "1.2"


def test_all_capture_content_modes_keep_the_field_set():
    for mode in ("full", "hash", "none"):
        record = _capture_record(capture_content=mode)
        assert set(record) == REQUIRED, mode
