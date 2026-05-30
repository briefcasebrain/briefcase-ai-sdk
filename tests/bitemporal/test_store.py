"""Tests for InMemoryBitemporalStore."""

from datetime import datetime, timezone, timedelta

import pytest

from briefcase.bitemporal import (
    BitemporalRecord,
    BitemporalStore,
    InMemoryBitemporalStore,
)


UTC = timezone.utc


def _ts(seconds: int = 0) -> datetime:
    return datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=seconds)


def test_in_memory_store_satisfies_protocol():
    # runtime_checkable Protocol — structural check.
    assert isinstance(InMemoryBitemporalStore(), BitemporalStore)


def test_append_and_history_preserves_insertion_order():
    store = InMemoryBitemporalStore()
    r1 = BitemporalRecord.new(key="k", valid_time=_ts(0), value=1, source="s",
                              transaction_time=_ts(0))
    r2 = BitemporalRecord.new(key="k", valid_time=_ts(1), value=2, source="s",
                              transaction_time=_ts(1))
    store.append(r1)
    store.append(r2)
    hist = store.history("k")
    assert [r.value for r in hist] == [1, 2]


def test_append_rejects_duplicate_record_id():
    store = InMemoryBitemporalStore()
    r = BitemporalRecord.new(key="k", valid_time=_ts(), value=1, source="s")
    store.append(r)
    with pytest.raises(ValueError):
        store.append(r)  # same record_id


def test_latest_returns_most_recent_transaction_time():
    store = InMemoryBitemporalStore()
    r1 = BitemporalRecord.new(
        key="k", valid_time=_ts(0), value=1, source="s", transaction_time=_ts(0)
    )
    r2 = BitemporalRecord.new(
        key="k", valid_time=_ts(10), value=2, source="s", transaction_time=_ts(10)
    )
    store.append(r1)
    store.append(r2)
    latest = store.latest("k")
    assert latest is not None and latest.value == 2


def test_as_of_clamps_transaction_time():
    store = InMemoryBitemporalStore()
    r1 = BitemporalRecord.new(
        key="k", valid_time=_ts(0), value=1, source="s", transaction_time=_ts(0)
    )
    r2 = BitemporalRecord.new(
        key="k", valid_time=_ts(10), value=2, source="s", transaction_time=_ts(10)
    )
    store.append_many([r1, r2])

    # Clamp before r2's transaction time — r1 wins.
    seen = store.as_of("k", transaction_time=_ts(5))
    assert seen is not None and seen.value == 1

    # At or after r2's transaction time — r2 wins.
    seen = store.as_of("k", transaction_time=_ts(10))
    assert seen is not None and seen.value == 2


def test_as_of_returns_none_for_unknown_key():
    store = InMemoryBitemporalStore()
    assert store.as_of("missing", transaction_time=_ts()) is None


def test_as_of_prefers_correction_on_tie():
    # Same transaction time, same valid time — the one with parent_record_id
    # set is the correction and should win.
    store = InMemoryBitemporalStore()
    r1 = BitemporalRecord.new(
        key="k", valid_time=_ts(0), value=1, source="s", transaction_time=_ts(5)
    )
    r2 = BitemporalRecord.new(
        key="k",
        valid_time=_ts(0),
        value=2,
        source="s",
        transaction_time=_ts(5),
        parent_record_id=r1.record_id,
    )
    store.append(r1)
    store.append(r2)
    seen = store.as_of("k", transaction_time=_ts(5))
    assert seen is not None and seen.value == 2
