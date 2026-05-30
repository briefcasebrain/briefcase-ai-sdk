"""
SQLite-backed :class:`BitemporalStore` — the OSS durable reference.

Zero-setup persistence for single-node production and for the walkthrough
scripts that need durability across process restarts. Built on the
stdlib ``sqlite3`` module, so the dependency footprint is the same as
core Python.

Schema matches the target schema in ``docs/design/bitemporal-native.md``
so the future Rust-core implementation is a drop-in replacement (same
columns, same indexes, same triggers). Append-only enforcement is at
the DB layer via BEFORE UPDATE / BEFORE DELETE triggers — the store
cannot be corrupted by discipline lapses elsewhere in the application.

Concurrency
-----------
SQLite's default write serialization is sufficient for single-writer
workloads. For multi-writer setups, enable WAL
(``pragma journal_mode=wal``) and coordinate writers at the application
layer. For true multi-writer analytics, use
:class:`IcebergBitemporalBackend` or the enterprise Postgres backend.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from briefcase.bitemporal.record import BitemporalRecord


_SCHEMA = """
CREATE TABLE IF NOT EXISTS bitemporal_records (
    record_id           TEXT PRIMARY KEY,
    key                 TEXT NOT NULL,
    valid_time          TEXT NOT NULL,
    transaction_time    TEXT NOT NULL,
    value_json          TEXT NOT NULL,
    source              TEXT,
    decision_id         TEXT,
    source_trust_level  TEXT,
    parent_record_id    TEXT REFERENCES bitemporal_records(record_id),
    metadata_json       TEXT,
    content_hash        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bt_key_tx
    ON bitemporal_records(key, transaction_time);
CREATE INDEX IF NOT EXISTS idx_bt_key_valid
    ON bitemporal_records(key, valid_time);
CREATE INDEX IF NOT EXISTS idx_bt_parent
    ON bitemporal_records(parent_record_id);

CREATE TRIGGER IF NOT EXISTS bitemporal_no_update
BEFORE UPDATE ON bitemporal_records
BEGIN
    SELECT RAISE(ABORT, 'bitemporal_records is append-only');
END;

CREATE TRIGGER IF NOT EXISTS bitemporal_no_delete
BEFORE DELETE ON bitemporal_records
BEGIN
    SELECT RAISE(ABORT, 'bitemporal_records is append-only');
END;
"""


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row_to_record(row: sqlite3.Row) -> BitemporalRecord:
    return BitemporalRecord(
        record_id=row["record_id"],
        key=row["key"],
        valid_time=_parse_iso(row["valid_time"]),
        transaction_time=_parse_iso(row["transaction_time"]),
        value=json.loads(row["value_json"]) if row["value_json"] is not None else None,
        source=row["source"],
        source_trust_level=row["source_trust_level"],
        decision=row["decision_id"],
        parent_record_id=row["parent_record_id"],
        metadata=(
            json.loads(row["metadata_json"])
            if row["metadata_json"] is not None
            else {}
        ),
    )


class SqliteBitemporalBackend:
    """Durable SQLite-backed :class:`BitemporalStore`.

    Parameters
    ----------
    path
        Filesystem path to the SQLite database. ``":memory:"`` for an
        ephemeral store suitable for tests.
    wal
        If True, enables WAL journaling on file-backed databases. WAL
        improves concurrent-reader performance at the cost of a separate
        journal file next to the database.
    """

    def __init__(self, path: str, *, wal: bool = True) -> None:
        self.path = path
        self._lock = threading.RLock()
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(
            path,
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,  # autocommit; we manage explicit BEGIN
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if wal and path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "SqliteBitemporalBackend":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def append(self, record: BitemporalRecord) -> None:
        self.append_many([record])

    def append_many(self, records: Iterable[BitemporalRecord]) -> None:
        records = list(records)
        if not records:
            return
        rows = [self._record_to_params(r) for r in records]
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                self._conn.executemany(
                    """
                    INSERT INTO bitemporal_records (
                        record_id, key, valid_time, transaction_time,
                        value_json, source, decision_id, source_trust_level,
                        parent_record_id, metadata_json, content_hash
                    ) VALUES (
                        :record_id, :key, :valid_time, :transaction_time,
                        :value_json, :source, :decision_id, :source_trust_level,
                        :parent_record_id, :metadata_json, :content_hash
                    )
                    """,
                    rows,
                )
                self._conn.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self._conn.execute("ROLLBACK")
                raise ValueError(str(exc)) from exc
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _record_to_params(r: BitemporalRecord) -> Dict[str, Any]:
        return {
            "record_id": r.record_id,
            "key": r.key,
            "valid_time": _iso(r.valid_time),
            "transaction_time": _iso(r.transaction_time),
            "value_json": json.dumps(r.value, sort_keys=True, default=str),
            "source": r.source,
            "decision_id": r.decision,
            "source_trust_level": r.source_trust_level,
            "parent_record_id": r.parent_record_id,
            "metadata_json": json.dumps(r.metadata or {}, sort_keys=True, default=str),
            "content_hash": r.content_hash(),
        }

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def history(self, key: str) -> List[BitemporalRecord]:
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT * FROM bitemporal_records
                WHERE key = ?
                ORDER BY transaction_time, valid_time
                """,
                (key,),
            )
            return [_row_to_record(row) for row in cur.fetchall()]

    def latest(self, key: str) -> Optional[BitemporalRecord]:
        return self.as_of(key)

    def as_of(
        self,
        key: str,
        *,
        transaction_time: Optional[datetime] = None,
        valid_time: Optional[datetime] = None,
    ) -> Optional[BitemporalRecord]:
        sql = ["SELECT * FROM bitemporal_records WHERE key = ?"]
        params: List[Any] = [key]
        if transaction_time is not None:
            sql.append("AND transaction_time <= ?")
            params.append(_iso(transaction_time))
        if valid_time is not None:
            sql.append("AND valid_time <= ?")
            params.append(_iso(valid_time))
        # Order picks the correction-wins-over-corrected tuple; LIMIT 1.
        sql.append(
            "ORDER BY transaction_time DESC, valid_time DESC, "
            "(CASE WHEN parent_record_id IS NOT NULL THEN 1 ELSE 0 END) DESC "
            "LIMIT 1"
        )
        with self._lock:
            cur = self._conn.execute(" ".join(sql), params)
            row = cur.fetchone()
            return _row_to_record(row) if row else None

    def keys(self) -> List[str]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT DISTINCT key FROM bitemporal_records ORDER BY key"
            )
            return [row["key"] for row in cur.fetchall()]

    def __len__(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) AS n FROM bitemporal_records")
            return int(cur.fetchone()["n"])
