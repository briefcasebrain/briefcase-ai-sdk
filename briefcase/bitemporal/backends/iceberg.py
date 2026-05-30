"""
Apache Iceberg adapter for :class:`BitemporalStore`.

For analytical workloads where the evidence set outgrows a single node
— long-tail SAR narrative histories, per-customer transaction archives,
multi-year backtests — a lakehouse table format is the right tier.

This OSS adapter wraps `pyiceberg <https://py.iceberg.apache.org>`_ and
works against any pyiceberg-supported catalog: the bundled
``SqlCatalog`` (SQLite / Postgres), REST, Hive metastore, Nessie, or an
AWS Glue catalog via ``pyiceberg[glue]``. Commercial managed catalogs
(Snowflake Horizon, Databricks Unity, Confluent Tableflow) and the
associated auth plumbing live in ``briefcase-ai-sdk-enterprise``.

Design
------
* **Schema**: one row per :class:`BitemporalRecord`. Columns cover the
  full record surface plus JSON-encoded ``value`` and ``metadata``.
* **Partitioning**: daily partitions on ``valid_time`` so the common
  "history of key K" and "as-of date D" queries prune cleanly.
* **Append-only**: rows are never deleted or updated. Every write is an
  Iceberg append; a correction is a new row with a fresh
  ``transaction_time`` and ``parent_record_id``.
* **Reads**: ``history`` and ``as_of`` scan the table with Iceberg
  pushdown on ``key``. Candidate selection matches
  :class:`InMemoryBitemporalStore` — pick the record with the latest
  ``(transaction_time, valid_time, is_correction)`` tuple at or below
  the clamp.

Install
-------
``pip install briefcase-ai[bitemporal-iceberg]`` pulls in pyiceberg with
the SQLite-catalog extra. Point ``catalog_uri`` at a local file for
tests, or at any supported catalog in production.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from briefcase.bitemporal.record import BitemporalRecord


_INSTALL_HINT = (
    "pyiceberg is required. Install with "
    "'pip install briefcase-ai[bitemporal-iceberg]' "
    "or 'pip install pyiceberg[sql-sqlite]'."
)


def _require_pyiceberg() -> None:
    try:
        import pyiceberg  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise ImportError(_INSTALL_HINT) from exc


def _record_to_row(r: BitemporalRecord) -> Dict[str, Any]:
    return {
        "record_id": r.record_id,
        "key": r.key,
        "valid_time": r.valid_time,
        "transaction_time": r.transaction_time,
        "value_json": json.dumps(r.value, sort_keys=True, default=str),
        "source": r.source,
        "source_trust_level": r.source_trust_level,
        "decision": r.decision,
        "parent_record_id": r.parent_record_id,
        "metadata_json": json.dumps(r.metadata or {}, sort_keys=True, default=str),
    }


def _as_utc(dt: Any) -> datetime:
    if isinstance(dt, datetime):
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(dt))


def _row_to_record(row: Dict[str, Any]) -> BitemporalRecord:
    return BitemporalRecord(
        record_id=row["record_id"],
        key=row["key"],
        valid_time=_as_utc(row["valid_time"]),
        transaction_time=_as_utc(row["transaction_time"]),
        value=json.loads(row["value_json"]) if row.get("value_json") else None,
        source=row.get("source"),
        source_trust_level=row.get("source_trust_level"),
        decision=row.get("decision"),
        parent_record_id=row.get("parent_record_id"),
        metadata=json.loads(row["metadata_json"]) if row.get("metadata_json") else {},
    )


def _build_schema():
    from pyiceberg.schema import Schema
    from pyiceberg.types import (
        NestedField,
        StringType,
        TimestamptzType,
    )

    return Schema(
        NestedField(1, "record_id", StringType(), required=True),
        NestedField(2, "key", StringType(), required=True),
        NestedField(3, "valid_time", TimestamptzType(), required=True),
        NestedField(4, "transaction_time", TimestamptzType(), required=True),
        NestedField(5, "value_json", StringType(), required=False),
        NestedField(6, "source", StringType(), required=False),
        NestedField(7, "source_trust_level", StringType(), required=False),
        NestedField(8, "decision", StringType(), required=False),
        NestedField(9, "parent_record_id", StringType(), required=False),
        NestedField(10, "metadata_json", StringType(), required=False),
    )


def _build_partition_spec(schema):
    from pyiceberg.partitioning import PartitionField, PartitionSpec
    from pyiceberg.transforms import DayTransform

    valid_time_id = schema.find_field("valid_time").field_id
    return PartitionSpec(
        PartitionField(
            source_id=valid_time_id,
            field_id=1000,
            transform=DayTransform(),
            name="valid_time_day",
        )
    )


class IcebergBitemporalBackend:
    """Iceberg-backed :class:`BitemporalStore`.

    Construction is lazy — the catalog and table are opened on first use
    so tests and introspection do not require a live connection.

    Parameters
    ----------
    catalog_name
        pyiceberg catalog identifier.
    namespace
        Iceberg namespace (database).
    table
        Table name.
    warehouse
        Warehouse path (filesystem or object-store URI).
    catalog_uri
        Backend URI for the catalog (e.g. ``sqlite:///path/to/catalog.db``
        for SqlCatalog, or ``https://nessie.example.com/api/v2`` for
        Nessie). Passed through to ``load_catalog`` as ``uri=``.
    catalog_type
        Optional pyiceberg catalog type (``sql``, ``rest``, ``hive``,
        ``nessie``, ``glue``). If omitted, pyiceberg infers from
        ``catalog_uri``.
    **catalog_kwargs
        Additional kwargs forwarded to
        ``pyiceberg.catalog.load_catalog``.
    """

    def __init__(
        self,
        *,
        catalog_name: str = "briefcase",
        namespace: str = "briefcase",
        table: str = "bitemporal_evidence",
        warehouse: Optional[str] = None,
        catalog_uri: Optional[str] = None,
        catalog_type: Optional[str] = None,
        **catalog_kwargs: Any,
    ) -> None:
        # Legacy: earlier tests pass ``catalog=``. Accept both.
        if "catalog" in catalog_kwargs:
            catalog_name = catalog_kwargs.pop("catalog")

        self.catalog_name = catalog_name
        self.catalog = catalog_name  # backwards-compat alias
        self.namespace = namespace
        self.table = table
        self.warehouse = warehouse
        self.catalog_uri = catalog_uri
        self.catalog_type = catalog_type
        self._catalog_kwargs = dict(catalog_kwargs)
        self._catalog = None
        self._table = None

    # ------------------------------------------------------------------
    # Lazy catalog / table handles
    # ------------------------------------------------------------------

    def _get_catalog(self):
        if self._catalog is not None:
            return self._catalog
        _require_pyiceberg()
        from pyiceberg.catalog import load_catalog

        props: Dict[str, Any] = dict(self._catalog_kwargs)
        if self.warehouse is not None:
            props.setdefault("warehouse", self.warehouse)
        if self.catalog_uri is not None:
            props.setdefault("uri", self.catalog_uri)
        if self.catalog_type is not None:
            props.setdefault("type", self.catalog_type)
        self._catalog = load_catalog(self.catalog_name, **props)
        return self._catalog

    def _get_table(self):
        if self._table is not None:
            return self._table
        catalog = self._get_catalog()
        identifier = (self.namespace, self.table)
        try:
            self._table = catalog.load_table(identifier)
        except Exception:
            try:
                catalog.create_namespace(self.namespace)
            except Exception:
                pass  # namespace already exists
            schema = _build_schema()
            spec = _build_partition_spec(schema)
            self._table = catalog.create_table(
                identifier=identifier,
                schema=schema,
                partition_spec=spec,
            )
        return self._table

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def append(self, record: BitemporalRecord) -> None:
        self.append_many([record])

    def append_many(self, records: Iterable[BitemporalRecord]) -> None:
        records = list(records)
        if not records:
            return
        import pyarrow as pa

        table = self._get_table()
        rows = [_record_to_row(r) for r in records]
        arrow_table = pa.Table.from_pylist(rows, schema=table.schema().as_arrow())
        table.append(arrow_table)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _scan_for_key(self, key: str) -> List[BitemporalRecord]:
        from pyiceberg.expressions import EqualTo

        table = self._get_table()
        try:
            arrow = table.scan(row_filter=EqualTo("key", key)).to_arrow()
        except Exception:
            return []  # empty table / no snapshots yet
        return [_row_to_record(r) for r in arrow.to_pylist()]

    def history(self, key: str) -> List[BitemporalRecord]:
        rows = self._scan_for_key(key)
        rows.sort(key=lambda r: (r.transaction_time, r.valid_time))
        return rows

    def latest(self, key: str) -> Optional[BitemporalRecord]:
        return self.as_of(key)

    def as_of(
        self,
        key: str,
        *,
        transaction_time: Optional[datetime] = None,
        valid_time: Optional[datetime] = None,
    ) -> Optional[BitemporalRecord]:
        candidates: List[BitemporalRecord] = []
        for r in self._scan_for_key(key):
            if transaction_time is not None and r.transaction_time > transaction_time:
                continue
            if valid_time is not None and r.valid_time > valid_time:
                continue
            candidates.append(r)
        if not candidates:
            return None

        def sort_key(r: BitemporalRecord):
            return (
                r.transaction_time,
                r.valid_time,
                1 if r.parent_record_id else 0,
            )

        return max(candidates, key=sort_key)

    def keys(self) -> List[str]:
        try:
            arrow = self._get_table().scan(selected_fields=("key",)).to_arrow()
        except Exception:
            return []
        seen: Dict[str, None] = {}
        for k in arrow["key"].to_pylist():
            seen[k] = None
        return list(seen)
