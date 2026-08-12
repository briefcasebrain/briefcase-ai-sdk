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

from briefcase._logging import get_logger
from briefcase.bitemporal.record import BitemporalRecord

logger = get_logger(__name__)


def _utc(column: str) -> str:
    """SQL that compares a timestamp column in real time, not as raw text.

    ``strftime`` resolves the stored offset to UTC and keeps milliseconds;
    ``COALESCE`` keeps a value SQLite cannot parse comparable (as itself)
    instead of turning it into NULL, which would drop the row silently.
    """
    return f"COALESCE(strftime('%Y-%m-%d %H:%M:%f', {column}), {column})"


def _is_readonly_error(exc: BaseException) -> bool:
    """Whether a sqlite3 error is 'the database cannot be written to'."""
    if not isinstance(exc, sqlite3.Error):
        return False
    if getattr(exc, "sqlite_errorname", "").startswith("SQLITE_READONLY"):
        return True
    return "readonly" in str(exc).lower() or "read-only" in str(exc).lower()


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

CREATE TRIGGER IF NOT EXISTS bitemporal_no_delete
BEFORE DELETE ON bitemporal_records
BEGIN
    SELECT RAISE(ABORT, 'bitemporal_records is append-only');
END;
"""

# Kept separate from _SCHEMA: the timestamp-normalization pass drops and
# recreates this trigger around its representation rewrite.
_UPDATE_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS bitemporal_no_update
BEFORE UPDATE ON bitemporal_records
BEGIN
    SELECT RAISE(ABORT, 'bitemporal_records is append-only');
END;
"""


def _iso(dt: datetime) -> str:
    """Normalize to a UTC ISO-8601 string so TEXT order equals time order."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


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
        # Set when legacy non-UTC rows could not be rewritten (a read-only
        # store). Reads then normalize in SQL so answers stay correct.
        self._compare_in_utc = False
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
            try:
                self._conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.Error as exc:
                if not _is_readonly_error(exc):
                    raise
                logger.warning("%s is read-only; keeping its journal mode", path)
        self._conn.executescript(_SCHEMA)
        self._conn.executescript(_UPDATE_TRIGGER)
        self._normalize_legacy_timestamps()

    def _normalize_legacy_timestamps(self) -> None:
        """Rewrite valid_time/transaction_time strings stored by earlier
        versions (naive or non-UTC offsets) to the normalized UTC form, so
        the TEXT comparisons in as_of/history hold across all rows. The
        rewrite changes representation, not history, so the append-only
        UPDATE trigger is dropped for the transaction and recreated after."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT record_id, valid_time, transaction_time"
                " FROM bitemporal_records"
                " WHERE valid_time NOT LIKE '%+00:00'"
                " OR transaction_time NOT LIKE '%+00:00'"
            ).fetchall()
            if not rows:
                return
            updates = [
                (
                    _iso(_parse_iso(row["valid_time"])),
                    _iso(_parse_iso(row["transaction_time"])),
                    row["record_id"],
                )
                for row in rows
            ]
            try:
                self._conn.execute("BEGIN")
                self._conn.execute("DROP TRIGGER IF EXISTS bitemporal_no_update")
                self._conn.executemany(
                    "UPDATE bitemporal_records"
                    " SET valid_time = ?, transaction_time = ?"
                    " WHERE record_id = ?",
                    updates,
                )
                self._conn.execute(_UPDATE_TRIGGER.replace("IF NOT EXISTS ", ""))
                self._conn.execute("COMMIT")
            except Exception as exc:
                self._conn.execute("ROLLBACK")
                self._conn.executescript(_UPDATE_TRIGGER)
                if not _is_readonly_error(exc):
                    raise
                # An archived or read-only-mounted store still opens for
                # queries. The rows keep their original representation, so
                # reads normalize in SQL instead: slower (the comparison is no
                # longer a plain indexed column) but correct.
                self._compare_in_utc = True
                logger.warning(
                    "%s is read-only, so %d legacy timestamp row(s) keep their "
                    "original offsets; as_of/history normalize them per query "
                    "instead, which cannot use the timestamp index.",
                    self.path,
                    len(updates),
                )

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
        tt, vt = self._time_exprs()
        with self._lock:
            cur = self._conn.execute(
                f"""
                SELECT * FROM bitemporal_records
                WHERE key = ?
                ORDER BY {tt}, {vt}
                """,
                (key,),
            )
            return [_row_to_record(row) for row in cur.fetchall()]

    def _time_exprs(self):
        """(transaction_time, valid_time) SQL expressions to order/compare on."""
        if self._compare_in_utc:
            return _utc("transaction_time"), _utc("valid_time")
        return "transaction_time", "valid_time"

    def latest(self, key: str) -> Optional[BitemporalRecord]:
        return self.as_of(key)

    def as_of(
        self,
        key: str,
        *,
        transaction_time: Optional[datetime] = None,
        valid_time: Optional[datetime] = None,
    ) -> Optional[BitemporalRecord]:
        tt, vt = self._time_exprs()
        # The bound value is always well-formed _iso() output, so it needs the
        # normalization but not the COALESCE fallback (one placeholder, one
        # parameter).
        bound = "strftime('%Y-%m-%d %H:%M:%f', ?)" if self._compare_in_utc else "?"
        sql = ["SELECT * FROM bitemporal_records WHERE key = ?"]
        params: List[Any] = [key]
        if transaction_time is not None:
            sql.append(f"AND {tt} <= {bound}")
            params.append(_iso(transaction_time))
        if valid_time is not None:
            sql.append(f"AND {vt} <= {bound}")
            params.append(_iso(valid_time))
        # Order picks the correction-wins-over-corrected tuple; LIMIT 1.
        sql.append(
            f"ORDER BY {tt} DESC, {vt} DESC, "
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
