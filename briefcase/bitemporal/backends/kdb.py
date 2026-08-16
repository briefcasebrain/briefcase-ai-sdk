"""
kdb+ adapter for :class:`briefcase.bitemporal.BitemporalStore`.

kdb+ is the natural fit for tick-rate workloads: columnar, time-series
native, and with bitemporal semantics built into the engine rather than
bolted on. This backend maps :class:`BitemporalRecord` rows onto a q
table with grouped attributes on ``key`` and the two time axes, and
evaluates ``as_of`` ordering server-side so the native engine does the
work rather than Python.

Design
------
* ``append`` issues an ``upsert`` with ``transaction_time`` and
  ``valid_time`` stored as q timestamps.
* ``as_of`` runs a q select that filters on both axes and takes the
  latest ``(transaction_time, valid_time, is_correction)`` tuple, so
  corrections win ties against originals (mirrors the SQLite backend).
* ``history`` selects all rows for a key ordered by
  ``transaction_time`` then ``valid_time``.
* ``value`` and ``metadata`` are stored as JSON strings; kdb+ prefers
  typed columns, so latency-critical deployments should project
  ``value`` into per-source tables.

Install
-------
Requires the commercial ``pykx`` client:
``pip install briefcase-ai[kdb]`` or ``pip install pykx``.
The import is lazy, so this module loads without pykx installed and
fails with a clear error on first use.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from types import TracebackType
from typing import Any, List, Optional

from briefcase.bitemporal.record import BitemporalRecord


_INSTALL_HINT = (
    "pykx is required. Install with "
    "'pip install briefcase-ai[kdb]' "
    "or 'pip install pykx'."
)

_NAIVE_DT_MSG = "naive datetime rejected; use UTC"

# Values interpolated into q expressions must match this allowlist.
# Anything outside it (quotes, backslashes, whitespace incl. a trailing
# newline, ...) is rejected so caller data can never terminate a q string
# literal. fullmatch, not match: $ alone still accepts one trailing newline.
_Q_SYMBOL_RE = re.compile(r"[A-Za-z0-9._:/\-]+")

# q table names are interpolated as identifiers, not string literals,
# so they get a tighter allowlist (fullmatch for the same newline reason).
_Q_TABLE_RE = re.compile(r"[A-Za-z._][A-Za-z0-9._]*")


def _require_pykx() -> Any:
    try:
        import pykx
    except ImportError as exc:  # pragma: no cover
        raise ImportError(_INSTALL_HINT) from exc
    return pykx


def _validate_q_symbol(value: str, field: str) -> str:
    """Reject values that cannot be embedded safely in a q expression."""
    if not _Q_SYMBOL_RE.fullmatch(value):
        raise ValueError(
            f"{field} {value!r} contains characters outside the allowed set "
            f"[A-Za-z0-9._:/-] and cannot be used in a q query"
        )
    return value


def _ensure_utc(dt: datetime, field: str) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"{_NAIVE_DT_MSG} ({field})")
    return dt.astimezone(timezone.utc)


def _dt_to_q_timestamp(dt: datetime) -> Any:
    """Convert a UTC ``datetime`` to a pykx timestamp atom.

    Routes through q's own ``"timestamp"$`` cast on the ISO-8601 string
    so the server-side parser, not the Python client, decides the
    nanosecond representation. Python ``datetime`` tops out at
    microsecond precision; q preserves nanoseconds on round-trip within
    its own storage.
    """
    pykx = _require_pykx()
    utc = _ensure_utc(dt, "datetime")
    return pykx.q('"timestamp"$', utc.isoformat())


def _q_timestamp_to_dt(value: Any) -> datetime:
    """Convert a q ``timestamp`` returned from pykx back to a UTC datetime."""
    if isinstance(value, datetime):
        return _ensure_utc(value, "datetime")
    iso = value.py() if hasattr(value, "py") else value
    if isinstance(iso, datetime):
        return _ensure_utc(iso, "datetime")
    return datetime.fromisoformat(str(iso)).replace(tzinfo=timezone.utc)


def _row_to_record(row: dict) -> BitemporalRecord:
    value_json = row.get("value")
    metadata_json = row.get("metadata")
    return BitemporalRecord(
        record_id=str(row["record_id"]),
        key=str(row["key"]),
        valid_time=_q_timestamp_to_dt(row["valid_time"]),
        transaction_time=_q_timestamp_to_dt(row["transaction_time"]),
        value=json.loads(value_json) if value_json else None,
        source=str(row["source"]) if row.get("source") else "",
        source_trust_level=(
            str(row["source_trust_level"]) if row.get("source_trust_level") else None
        ),
        parent_record_id=(
            str(row["parent_record_id"]) if row.get("parent_record_id") else None
        ),
        metadata=json.loads(metadata_json) if metadata_json else {},
    )


class KdbBitemporalBackend:
    """kdb+-backed :class:`briefcase.bitemporal.BitemporalStore`.

    Conforms structurally to the ``BitemporalStore`` Protocol:
    ``append``, ``append_many``, ``history``, ``latest``, ``as_of``,
    ``keys``. Also exposes ``append_correction`` and ``query``, an
    alias of ``as_of``.

    Construction does not open a connection; the connection and schema
    are established lazily on first use.

    Parameters
    ----------
    host
        kdb+ host.
    port
        kdb+ port.
    table
        q table name. Defaults to ``"bitemporal_records"``.
    username / password
        Optional auth. Applied when opening the connection.
    tls
        If True, use TLS (``pykx.QConnection(tls=True)``).

    Timezone discipline
    -------------------
    All datetimes on the Python side MUST be timezone-aware and are
    normalized to UTC on ingress. Naive datetimes raise ``ValueError``.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5000,
        table: str = "bitemporal_records",
        username: Optional[str] = None,
        password: Optional[str] = None,
        tls: bool = False,
    ) -> None:
        if not _Q_TABLE_RE.fullmatch(table):
            raise ValueError(
                f"table {table!r} is not a valid q table name "
                f"(allowed: [A-Za-z._][A-Za-z0-9._]*)"
            )
        self.host = host
        self.port = port
        self.table = table
        self.username = username
        self.password = password
        self.tls = tls
        self._conn_obj: Optional[Any] = None
        self._schema_ready = False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _conn(self) -> Any:
        if self._conn_obj is None:
            pykx = _require_pykx()
            kwargs: dict = {"host": self.host, "port": self.port}
            if self.username is not None:
                kwargs["username"] = self.username
            if self.password is not None:
                kwargs["password"] = self.password
            if self.tls:
                kwargs["tls"] = True
            self._conn_obj = pykx.QConnection(**kwargs)
        if not self._schema_ready:
            self._ensure_schema()
            self._schema_ready = True
        return self._conn_obj

    def close(self) -> None:
        if self._conn_obj is not None:
            try:
                self._conn_obj.close()
            finally:
                self._conn_obj = None
                self._schema_ready = False

    def __enter__(self) -> "KdbBitemporalBackend":
        self._conn()
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Create the in-memory table and apply column attributes.

        Idempotent, so it is safe to call on every connection. The table
        is created only if absent; ``g#`` attributes re-apply harmlessly
        in kdb+.
        """
        assert self._conn_obj is not None
        create = (
            f"if[not `{self.table} in key `.;"
            f" {self.table}:([]"
            " record_id:`symbol$();"
            " key:`symbol$();"
            " valid_time:`timestamp$();"
            " transaction_time:`timestamp$();"
            " value:();"
            " source:`symbol$();"
            " source_trust_level:`symbol$();"
            " parent_record_id:`symbol$();"
            " metadata:())"
            "]"
        )
        self._conn_obj(create)
        # Grouped attributes make point lookups on key and range filters
        # on the two time axes hash rather than scan. The update form is
        # idiomatic q and idempotent.
        self._conn_obj(
            f"update `g#key, `g#valid_time, `g#transaction_time, "
            f"`g#record_id from `{self.table}"
        )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def append(self, record: BitemporalRecord) -> None:
        conn = self._conn()
        _ensure_utc(record.valid_time, "valid_time")
        _ensure_utc(record.transaction_time, "transaction_time")
        record_id = _validate_q_symbol(record.record_id, "record_id")
        _validate_q_symbol(record.key, "key")

        # Duplicate-record-id check.
        count = conn(
            f'count select from {self.table} where record_id=`$"{record_id}"'
        )
        count_int = int(count.py()) if hasattr(count, "py") else int(count)
        if count_int > 0:
            raise ValueError(f"record_id {record.record_id!r} already present")

        row = {
            "record_id": record.record_id,
            "key": record.key,
            "valid_time": _dt_to_q_timestamp(record.valid_time),
            "transaction_time": _dt_to_q_timestamp(record.transaction_time),
            "value": json.dumps(record.value, sort_keys=True, default=str),
            "source": record.source,
            "source_trust_level": record.source_trust_level or "",
            "parent_record_id": record.parent_record_id or "",
            "metadata": json.dumps(record.metadata or {}, sort_keys=True, default=str),
        }
        conn(f"{{ `{self.table} upsert x }}", row)

    def append_many(self, records: Iterable[BitemporalRecord]) -> None:
        for r in records:
            self.append(r)

    def append_correction(
        self,
        parent: BitemporalRecord,
        corrected_value: Any,
        corrected_at: datetime,
        source: str,
        source_trust_level: str = "verified",
        metadata: Optional[dict] = None,
    ) -> BitemporalRecord:
        """Append a correction for ``parent``.

        The returned record inherits ``key`` and ``valid_time`` from
        ``parent`` and points at it via ``parent_record_id``. Unlike the
        :func:`briefcase.bitemporal.corrections.append_correction`
        helper, which propagates the parent's ``source_trust_level``,
        this method honors the caller-supplied override so a correction
        can be marked e.g. ``"verified"`` even when the original source
        was ``"derived"``. The append-only invariant
        (``correction.transaction_time > parent.transaction_time``) is
        enforced identically.
        """
        _ensure_utc(corrected_at, "corrected_at")
        if corrected_at <= parent.transaction_time:
            raise ValueError(
                "correction.transaction_time must be strictly after the "
                "original's transaction_time"
            )
        merged_metadata = dict(parent.metadata or {})
        merged_metadata.update(metadata or {})
        merged_metadata.setdefault("correction_of", parent.record_id)
        correction = BitemporalRecord.new(
            key=parent.key,
            valid_time=parent.valid_time,
            value=corrected_value,
            source=source,
            transaction_time=corrected_at,
            source_trust_level=source_trust_level,
            parent_record_id=parent.record_id,
            metadata=merged_metadata,
        )
        self.append(correction)
        return correction

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _select_rows(self, q_expr: str) -> List[dict]:
        conn = self._conn()
        result = conn(q_expr)
        py_rows = result.py() if hasattr(result, "py") else result
        if not py_rows:
            return []
        if isinstance(py_rows, dict):
            # pykx returns dict-of-columns for flip'd tables.
            keys = list(py_rows.keys())
            rows: List[dict] = []
            for i in range(len(next(iter(py_rows.values())))):
                rows.append({k: py_rows[k][i] for k in keys})
            return rows
        return list(py_rows)

    def history(self, key: str) -> List[BitemporalRecord]:
        _validate_q_symbol(key, "key")
        rows = self._select_rows(
            f'`transaction_time`valid_time xasc '
            f'select from {self.table} where key=`$"{key}"'
        )
        return [_row_to_record(r) for r in rows]

    def latest(self, key: str) -> Optional[BitemporalRecord]:
        return self.as_of(key)

    def as_of(
        self,
        key: str,
        *,
        transaction_time: Optional[datetime] = None,
        valid_time: Optional[datetime] = None,
    ) -> Optional[BitemporalRecord]:
        _validate_q_symbol(key, "key")
        clauses = [f'key=`$"{key}"']
        if transaction_time is not None:
            _ensure_utc(transaction_time, "transaction_time")
            ts = transaction_time.astimezone(timezone.utc).isoformat()
            clauses.append(f'transaction_time<=`timestamp$"{ts}"')
        if valid_time is not None:
            _ensure_utc(valid_time, "valid_time")
            vs = valid_time.astimezone(timezone.utc).isoformat()
            clauses.append(f'valid_time<=`timestamp$"{vs}"')
        where = ", ".join(clauses)
        # ORDER BY transaction_time desc, valid_time desc, is_correction
        # desc LIMIT 1, so corrections win ties against originals
        # (mirrors the SQLite backend).
        q_expr = (
            f"1#`transaction_time`valid_time`is_correction xdesc "
            f"select transaction_time, valid_time, "
            f"is_correction:not null parent_record_id, "
            f"record_id, key, value, source, source_trust_level, "
            f"parent_record_id, metadata "
            f"from {self.table} where {where}"
        )
        rows = self._select_rows(q_expr)
        if not rows:
            return None
        return _row_to_record(rows[0])

    # Backward-compatible alias; the Protocol name is ``as_of``.
    query = as_of

    def keys(self) -> List[str]:
        rows = self._select_rows(f"distinct exec key from {self.table}")
        return [str(r) if not isinstance(r, dict) else str(r["key"]) for r in rows]


__all__ = ["KdbBitemporalBackend"]
