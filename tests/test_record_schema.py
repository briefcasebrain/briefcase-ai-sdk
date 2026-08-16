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
REQUIRED = set(FIXTURE["required_fields"])
OPTIONAL = set(FIXTURE["optional_fields"])


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
    assert REQUIRED.issubset(set(example))
    assert set(example) - REQUIRED <= OPTIONAL


def test_all_capture_content_modes_keep_the_field_set():
    for mode in ("full", "hash", "none"):
        record = _capture_record(capture_content=mode)
        assert set(record) == REQUIRED, mode
