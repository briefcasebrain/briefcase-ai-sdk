"""SqliteBitemporalBackend — durable reference backend."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

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


def test_as_of_orders_mixed_offset_timestamps_chronologically():
    """TEXT comparison of stored timestamps must equal chronological order
    even when records carry different UTC offsets."""
    store = SqliteBitemporalBackend(":memory:", wal=False)
    ist = timezone(timedelta(hours=5, minutes=30))
    earlier = BitemporalRecord.new(
        key="k",
        valid_time=datetime(2026, 1, 1, tzinfo=UTC),
        value={"px": 1.0},
        source="test",
        # 10:00+05:30 is 04:30 UTC
        transaction_time=datetime(2026, 1, 1, 10, 0, tzinfo=ist),
    )
    later = BitemporalRecord.new(
        key="k",
        valid_time=datetime(2026, 1, 1, tzinfo=UTC),
        value={"px": 2.0},
        source="test",
        transaction_time=datetime(2026, 1, 1, 5, 0, tzinfo=UTC),
    )
    store.append_many([earlier, later])

    # Live view: the 05:00 UTC record is the latest belief.
    assert store.latest("k").value == {"px": 2.0}

    # Clamp between the two instants: only the 04:30 UTC record is visible.
    mid = store.as_of(
        "k", transaction_time=datetime(2026, 1, 1, 4, 45, tzinfo=UTC)
    )
    assert mid is not None
    assert mid.value == {"px": 1.0}

    # History is chronological, not lexicographic.
    hist = store.history("k")
    assert [r.value["px"] for r in hist] == [1.0, 2.0]


def test_asof_view_wraps_sqlite_backend():
    store = SqliteBitemporalBackend(":memory:", wal=False)
    r1 = _record(px=1.0)
    store.append(r1)
    with AsOfView(store, transaction_time=datetime(2030, 1, 1, tzinfo=UTC)) as view:
        assert view.latest("k").record_id == r1.record_id


def test_open_normalizes_legacy_timestamp_rows(tmp_path):
    """Rows written by earlier versions with non-UTC offsets are rewritten to
    the normalized UTC form on open, so as_of TEXT comparisons stay correct."""
    import sqlite3

    db = str(tmp_path / "bt.db")
    SqliteBitemporalBackend(db, wal=False).close()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO bitemporal_records "
        "(record_id, key, valid_time, transaction_time, value_json, content_hash) "
        "VALUES ('r1', 'k', '2026-01-01T10:00:00+05:30', "
        "'2026-01-01T10:00:00+05:30', '{\"px\": 1.0}', 'h')"
    )
    conn.commit()
    conn.close()

    store = SqliteBitemporalBackend(db, wal=False)

    # 10:00+05:30 is 04:30 UTC, so it is visible as of 05:00 UTC.
    rec = store.as_of("k", transaction_time=datetime(2026, 1, 1, 5, 0, tzinfo=UTC))
    assert rec is not None
    assert rec.record_id == "r1"

    # The stored string is now the normalized form.
    row = store._conn.execute(
        "SELECT transaction_time FROM bitemporal_records WHERE record_id = 'r1'"
    ).fetchone()
    assert row["transaction_time"] == "2026-01-01T04:30:00+00:00"

    # The append-only triggers are back in force after the rewrite.
    with pytest.raises(Exception, match="append-only"):
        store._conn.execute("UPDATE bitemporal_records SET key = 'x'")
    store.close()


def _seed_legacy_offsets(db):
    """Two rows whose stored offsets order the opposite way as text."""
    import sqlite3

    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO bitemporal_records "
        "(record_id, key, valid_time, transaction_time, value_json, content_hash) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            # 10:00+05:30 is 04:30 UTC (earlier).
            ("early", "k", "2026-01-01T10:00:00+05:30", "2026-01-01T10:00:00+05:30",
             '{"px": 1.0}', "h1"),
            # 06:00+00:00 is 06:00 UTC (later), but sorts *before* as raw text.
            ("late", "k", "2026-01-01T06:00:00+00:00", "2026-01-01T06:00:00+00:00",
             '{"px": 2.0}', "h2"),
        ],
    )
    conn.commit()
    conn.close()


def test_read_only_store_answers_as_of_correctly(tmp_path):
    """A read-only store cannot rewrite legacy offsets, so the comparison must
    normalize at query time. Raw TEXT ordering puts 06:00+00:00 before
    10:00+05:30 even though it is 90 minutes later in real time."""
    import os

    db = str(tmp_path / "ro_correct.db")
    SqliteBitemporalBackend(db, wal=False).close()
    _seed_legacy_offsets(db)
    os.chmod(db, 0o444)

    try:
        store = SqliteBitemporalBackend(db, wal=False)
        # As of 05:00 UTC only the 04:30 UTC row exists.
        rec = store.as_of("k", transaction_time=datetime(2026, 1, 1, 5, 0, tzinfo=UTC))
        assert rec is not None and rec.record_id == "early"

        # As of 07:00 UTC both exist; the latest is the 06:00 UTC row.
        rec = store.as_of("k", transaction_time=datetime(2026, 1, 1, 7, 0, tzinfo=UTC))
        assert rec is not None and rec.record_id == "late"
        store.close()
    finally:
        os.chmod(db, 0o644)


def test_writable_store_still_normalizes_on_open(tmp_path):
    """The rewrite stays the fast path: a writable store stores normalized
    text, so queries keep comparing an indexable column directly."""
    db = str(tmp_path / "rw.db")
    SqliteBitemporalBackend(db, wal=False).close()
    _seed_legacy_offsets(db)

    store = SqliteBitemporalBackend(db, wal=False)
    rows = store._conn.execute(
        "SELECT transaction_time FROM bitemporal_records ORDER BY record_id"
    ).fetchall()
    assert [r["transaction_time"] for r in rows] == [
        "2026-01-01T04:30:00+00:00",
        "2026-01-01T06:00:00+00:00",
    ]
    rec = store.as_of("k", transaction_time=datetime(2026, 1, 1, 7, 0, tzinfo=UTC))
    assert rec is not None and rec.record_id == "late"
    store.close()


def test_read_only_store_opens_for_queries(tmp_path):
    """An archived or read-only-mounted audit database must still open. The
    legacy-timestamp rewrite is a repair, not a precondition for reading."""
    import os
    import sqlite3

    db = str(tmp_path / "ro.db")
    SqliteBitemporalBackend(db, wal=False).close()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO bitemporal_records "
        "(record_id, key, valid_time, transaction_time, value_json, content_hash) "
        "VALUES ('r1', 'k', '2026-01-01T10:00:00+05:30', "
        "'2026-01-01T10:00:00+05:30', '{\"px\": 1.0}', 'h')"
    )
    conn.commit()
    conn.close()
    os.chmod(db, 0o444)

    try:
        store = SqliteBitemporalBackend(db, wal=False)
        assert store.as_of("k", transaction_time=datetime(2026, 1, 1, 23, 0, tzinfo=UTC)) is not None
        store.close()
    finally:
        os.chmod(db, 0o644)
