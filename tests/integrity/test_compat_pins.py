"""Byte pins for the private canonical-JSON helpers that hash stored data.

``BitemporalRecord.content_hash`` and the examiner bundle's ``content_hash``
are derived from these encodings, so their bytes are load-bearing: any
change silently invalidates every previously stored hash. These pins were
written against the shipped implementations before they were re-pointed at
``briefcase.integrity.canonical_json_compat``; they must pass unchanged on
both sides of that consolidation.
"""

from __future__ import annotations

from datetime import datetime, timezone

from briefcase.bitemporal.record import _canonical_json as _bitemporal_canonical
from briefcase.compliance.examiner import _canonical_json as _examiner_canonical

# Exercises key sorting, non-ASCII escaping, datetime via default=str,
# and nested containers in one value.
_VALUE = {
    "b": 1,
    "a": "café",
    "nested": {"y": [1, 2, None], "x": True},
    "when": datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc),
}

# json.dumps(sort_keys=True, default=str, separators=(",", ":")) with the
# default ensure_ascii=True: "café" is escaped, datetime renders via str()
# (space separator, +00:00 offset).
_EXPECTED = (
    '{"a":"caf\\u00e9","b":1,"nested":{"x":true,"y":[1,2,null]},'
    '"when":"2026-01-02 03:04:05.678901+00:00"}'
)


def test_bitemporal_canonical_bytes_are_pinned() -> None:
    assert _bitemporal_canonical(_VALUE) == _EXPECTED


def test_examiner_canonical_bytes_are_pinned() -> None:
    assert _examiner_canonical(_VALUE) == _EXPECTED


def test_both_helpers_agree_byte_for_byte() -> None:
    assert _bitemporal_canonical(_VALUE) == _examiner_canonical(_VALUE)
