"""SqliteBitemporalBackend — durable reference backend."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

import pytest

from briefcase.bitemporal import (
    AsOfView,
    BitemporalRecord,
    BitemporalStore,
    append_correction,
)
from briefcase.bitemporal.backends import SqliteBitemporalBackend


UTC = timezone.utc


def _record(key: str = "k", px: float = 1.0) -> BitemporalRecord:
    return BitemporalRecord.new(
        key=key,
        valid_time=datetime(2026, 4, 17, tzinfo=UTC),
        value={"px": px},
        source="test",
        source_trust_level="primary",
    )


def test_memory_backend_round_trip():
    store = SqliteBitemporalBackend(":memory:", wal=False)
    r = _record()
    store.append(r)
    assert store.latest("k").record_id == r.record_id
    assert store.latest("k").value == {"px": 1.0}
    assert store.keys() == ["k"]
    assert len(store) == 1


def test_file_backend_survives_reopen():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bt.db")
        with SqliteBitemporalBackend(path) as a:
            a.append(_record())
        with SqliteBitemporalBackend(path) as b:
            assert b.latest("k") is not None
            assert len(b) == 1


def test_satisfies_bitemporal_store_protocol():
    store = SqliteBitemporalBackend(":memory:", wal=False)
    assert isinstance(store, BitemporalStore)


def test_append_only_trigger_blocks_update():
    store = SqliteBitemporalBackend(":memory:", wal=False)
    store.append(_record())
    with pytest.raises(Exception, match="append-only"):
        store._conn.execute(
            "UPDATE bitemporal_records SET source='tamper' WHERE key='k'"
        )


def test_append_only_trigger_blocks_delete():
    store = SqliteBitemporalBackend(":memory:", wal=False)
    store.append(_record())
    with pytest.raises(Exception, match="append-only"):
        store._conn.execute("DELETE FROM bitemporal_records WHERE key='k'")


def test_duplicate_record_id_rejected():
    store = SqliteBitemporalBackend(":memory:", wal=False)
    r = _record()
    store.append(r)
    with pytest.raises(ValueError):
        store.append(r)


def test_as_of_filters_transaction_time():
    store = SqliteBitemporalBackend(":memory:", wal=False)
    r1 = BitemporalRecord.new(
        key="k",
        valid_time=datetime(2026, 4, 17, tzinfo=UTC),
        value={"px": 1.0001},
        source="test",
        transaction_time=datetime(2026, 4, 17, tzinfo=UTC),
    )
    store.append(r1)
    correction = append_correction(
        store, r1,
        corrected_value={"px": 1.0002},
        transaction_time=datetime(2026, 5, 17, tzinfo=UTC),
    )

    # Live view — correction wins.
    assert store.latest("k").value == {"px": 1.0002}

    # Clamp to a day before the correction landed.
    early = store.as_of("k", transaction_time=datetime(2026, 5, 1, tzinfo=UTC))
    assert early.value == {"px": 1.0001}
    assert early.record_id == r1.record_id

    # Clamp to a day after — correction is visible.
    late = store.as_of("k", transaction_time=datetime(2026, 6, 1, tzinfo=UTC))
    assert late.record_id == correction.record_id


def test_asof_view_wraps_sqlite_backend():
    store = SqliteBitemporalBackend(":memory:", wal=False)
    r1 = _record(px=1.0)
    store.append(r1)
    with AsOfView(store, transaction_time=datetime(2030, 1, 1, tzinfo=UTC)) as view:
        assert view.latest("k").record_id == r1.record_id
