"""Tests for BitemporalRecord."""

from datetime import datetime, timezone, timedelta

import pytest

from briefcase.bitemporal import BitemporalRecord


UTC = timezone.utc


def _ts(seconds: int = 0) -> datetime:
    return datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=seconds)


def test_new_assigns_defaults():
    r = BitemporalRecord.new(
        key="USDC/USD", valid_time=_ts(), value=1.0001, source="bloomberg"
    )
    assert r.key == "USDC/USD"
    assert r.value == 1.0001
    assert r.source == "bloomberg"
    assert r.transaction_time.tzinfo is UTC
    assert r.record_id  # uuid assigned
    assert r.parent_record_id is None


def test_valid_time_must_be_tzaware():
    naive = datetime(2026, 4, 17, 12, 0, 0)
    with pytest.raises(ValueError):
        BitemporalRecord.new(key="k", valid_time=naive, value=1, source="s")


def test_transaction_time_must_be_tzaware_when_supplied():
    naive = datetime(2026, 4, 17, 12, 0, 0)
    with pytest.raises(ValueError):
        BitemporalRecord.new(
            key="k", valid_time=_ts(), value=1, source="s", transaction_time=naive
        )


def test_content_hash_stable_across_construction():
    r1 = BitemporalRecord.new(
        key="k", valid_time=_ts(), value={"a": 1, "b": 2}, source="s",
    )
    r2 = BitemporalRecord.new(
        key="k", valid_time=_ts(), value={"b": 2, "a": 1}, source="s",
    )
    # Different UUIDs, same content hash.
    assert r1.record_id != r2.record_id
    assert r1.content_hash() == r2.content_hash()


def test_roundtrip_to_dict_from_dict():
    r = BitemporalRecord.new(
        key="k",
        valid_time=_ts(),
        value={"x": 1},
        source="s",
        decision="dec-123",
        source_trust_level="primary",
        metadata={"feed": "v2"},
    )
    again = BitemporalRecord.from_dict(r.to_dict())
    assert again == r


def test_record_is_frozen():
    r = BitemporalRecord.new(key="k", valid_time=_ts(), value=1, source="s")
    with pytest.raises(Exception):
        r.value = 2  # type: ignore[misc]
