"""Canonical JSON profile matrix."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from briefcase.integrity import canonical_json, canonical_json_compat, sha256_hex


def test_sorted_keys_and_tight_separators() -> None:
    assert canonical_json({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_nested_dicts_sort_recursively() -> None:
    forward = {"outer": {"y": 2, "x": 1}, "a": [3, {"k": 1, "j": 2}]}
    reordered = {"a": [3, {"j": 2, "k": 1}], "outer": {"x": 1, "y": 2}}
    assert canonical_json(forward) == canonical_json(reordered)


def test_non_ascii_stays_utf8() -> None:
    assert canonical_json({"s": "café"}) == '{"s":"café"}'.encode("utf-8")


def test_strict_profile_rejects_non_serializable() -> None:
    with pytest.raises(TypeError):
        canonical_json({"when": datetime(2026, 1, 1, tzinfo=timezone.utc)})


def test_strict_profile_rejects_nan_and_infinity() -> None:
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})
    with pytest.raises(ValueError):
        canonical_json({"x": float("inf")})


def test_fallback_str_serializes_datetimes() -> None:
    value = {"when": datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)}
    assert canonical_json(value, fallback=str) == b'{"when":"2026-01-02 03:04:05+00:00"}'


def test_compat_profile_escapes_ascii_and_coerces() -> None:
    value = {"s": "café", "when": datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)}
    assert (
        canonical_json_compat(value)
        == '{"s":"caf\\u00e9","when":"2026-01-02 03:04:05+00:00"}'
    )


def test_sha256_hex() -> None:
    assert sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
