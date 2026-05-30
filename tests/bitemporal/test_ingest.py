"""Batch and stream ingestion produce equivalent as-of results."""

from datetime import datetime, timezone, timedelta

import pytest

from briefcase.bitemporal import (
    BitemporalRecord,
    InMemoryBitemporalStore,
    AsOfView,
    batch_append,
    stream_append,
)


UTC = timezone.utc


def _ts(seconds: int = 0) -> datetime:
    return datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=seconds)


def _build_records():
    return [
        BitemporalRecord.new(key=f"k{i}", valid_time=_ts(i), value=i, source="feed")
        for i in range(5)
    ]


def test_batch_append_shares_transaction_time():
    store = InMemoryBitemporalStore()
    batch_ts = _ts(500)
    batch_append(store, _build_records(), transaction_time=batch_ts)
    for k in store.keys():
        for r in store.history(k):
            assert r.transaction_time == batch_ts


def test_batch_append_requires_tzaware_transaction_time():
    store = InMemoryBitemporalStore()
    with pytest.raises(ValueError):
        batch_append(
            store, _build_records(),
            transaction_time=datetime(2026, 4, 17),
        )


def test_stream_append_preserves_supplied_transaction_time():
    store = InMemoryBitemporalStore()
    r = BitemporalRecord.new(
        key="k", valid_time=_ts(0), value=1, source="feed",
        transaction_time=_ts(42),
    )
    stream_append(store, r)
    assert store.latest("k").transaction_time == _ts(42)


def test_batch_and_stream_produce_equivalent_latest():
    """Same inputs, two ingestion paths, identical AsOfView reads."""
    records = _build_records()

    batch_store = InMemoryBitemporalStore()
    batch_append(batch_store, records, transaction_time=_ts(1000))

    stream_store = InMemoryBitemporalStore()
    for r in records:
        # Stream path: each record gets its own transaction_time <= 1000.
        stream_append(
            stream_store,
            BitemporalRecord.new(
                key=r.key, valid_time=r.valid_time, value=r.value, source=r.source,
                transaction_time=_ts(500 + r.value),  # staggered
            ),
        )

    # An as-of view taken after both batches have landed sees the same
    # latest value per key.
    with AsOfView(batch_store, transaction_time=_ts(2000)) as b, \
         AsOfView(stream_store, transaction_time=_ts(2000)) as s:
        for k in records:
            assert b.latest(k.key).value == s.latest(k.key).value
