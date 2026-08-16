"""Unit tests for :class:`briefcase.bitemporal.backends.iceberg_glue.GlueIcebergBackend`.

Uses ``moto.mock_aws`` to stub out Glue + S3. pyiceberg itself is only
imported lazily by the parent class's ``_get_catalog``, so construction
(which only touches Glue for the database existence check) works with a
moto-only environment and does not require a live pyiceberg install.

The happy-path append/history test bypasses pyiceberg by monkeypatching
the parent's ``_get_table`` with a minimal in-memory fake, enough to
verify that the inherited methods dispatch correctly through the
Glue-auth wrapper.

Skips cleanly when boto3 or moto is not installed (dev extra).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import pytest

boto3 = pytest.importorskip("boto3")
pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from briefcase.bitemporal import BitemporalRecord  # noqa: E402
from briefcase.bitemporal.backends.iceberg_glue import GlueIcebergBackend  # noqa: E402

UTC = timezone.utc

_PROTOCOL_METHODS = (
    "append",
    "append_many",
    "history",
    "latest",
    "as_of",
    "keys",
)


@pytest.fixture
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def mocked_aws(aws_credentials: None) -> Iterator[None]:
    with mock_aws():
        yield


def test_missing_database_raises(mocked_aws: None) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        GlueIcebergBackend(
            database="nonexistent_db",
            table="evidence",
            s3_warehouse="s3://test-bucket/warehouse",
            region="us-east-1",
        )


def test_existing_database_accepted(mocked_aws: None) -> None:
    glue = boto3.client("glue", region_name="us-east-1")
    glue.create_database(DatabaseInput={"Name": "testdb"})

    # Construction must succeed; catalog creation is lazy in the parent.
    backend = GlueIcebergBackend(
        database="testdb",
        table="evidence",
        s3_warehouse="s3://test-bucket/warehouse",
        region="us-east-1",
    )
    assert backend.namespace == "testdb"
    assert backend.table == "evidence"


def test_protocol_conformance_structural() -> None:
    for method in _PROTOCOL_METHODS:
        assert callable(getattr(GlueIcebergBackend, method, None)), method


def test_close_clears_catalog_refs(mocked_aws: None) -> None:
    glue = boto3.client("glue", region_name="us-east-1")
    glue.create_database(DatabaseInput={"Name": "testdb"})
    backend = GlueIcebergBackend(
        database="testdb",
        table="evidence",
        s3_warehouse="s3://test-bucket/warehouse",
        region="us-east-1",
    )
    # Touch internal fields to simulate an opened catalog.
    backend._catalog = object()
    backend._table = object()
    backend.close()
    assert backend._catalog is None
    assert backend._table is None


def test_role_assumption_passes_credentials(mocked_aws: None) -> None:
    glue = boto3.client("glue", region_name="us-east-1")
    glue.create_database(DatabaseInput={"Name": "testdb"})
    backend = GlueIcebergBackend(
        database="testdb",
        table="evidence",
        s3_warehouse="s3://test-bucket/warehouse",
        region="us-east-1",
        role_arn="arn:aws:iam::123456789012:role/lake-access",
    )
    # STS credentials must be forwarded to pyiceberg's catalog kwargs.
    assert "s3.access-key-id" in backend._catalog_kwargs
    assert "s3.secret-access-key" in backend._catalog_kwargs


class _FakeScan:
    def __init__(self, rows: list[dict[str, Any]], key_filter: str | None) -> None:
        if key_filter is not None:
            self._rows = [r for r in rows if r["key"] == key_filter]
        else:
            self._rows = list(rows)

    def to_arrow(self) -> Any:
        import pyarrow as pa

        return pa.Table.from_pylist(self._rows) if self._rows else pa.table({})


class _FakeIcebergTable:
    """Enough of the pyiceberg ``Table`` surface for append + scan.

    The parent class calls ``schema().as_arrow()`` to shape the incoming
    arrow batch, then ``append(arrow_table)`` and
    ``scan(row_filter=...).to_arrow()``.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def schema(self) -> Any:
        import pyarrow as pa

        class _S:
            @staticmethod
            def as_arrow() -> Any:
                return pa.schema(
                    [
                        pa.field("record_id", pa.string()),
                        pa.field("key", pa.string()),
                        pa.field("valid_time", pa.timestamp("us", tz="UTC")),
                        pa.field("transaction_time", pa.timestamp("us", tz="UTC")),
                        pa.field("value_json", pa.string()),
                        pa.field("source", pa.string()),
                        pa.field("source_trust_level", pa.string()),
                        pa.field("decision", pa.string()),
                        pa.field("parent_record_id", pa.string()),
                        pa.field("metadata_json", pa.string()),
                    ]
                )

        return _S()

    def append(self, arrow_table: Any) -> None:
        self.rows.extend(arrow_table.to_pylist())

    def scan(self, row_filter: Any = None) -> _FakeScan:
        # The parent always filters by ``EqualTo("key", <value>)``. Grab
        # the literal off whichever pyiceberg version exposes it.
        key_value = getattr(row_filter, "literal", None)
        if key_value is not None and hasattr(key_value, "value"):
            key_value = key_value.value
        return _FakeScan(self.rows, key_value if isinstance(key_value, str) else None)


def test_inherited_append_and_history_roundtrip(
    mocked_aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the inherited append/history path against an in-memory fake."""
    pytest.importorskip("pyarrow")
    pytest.importorskip("pyiceberg")
    glue = boto3.client("glue", region_name="us-east-1")
    glue.create_database(DatabaseInput={"Name": "testdb"})

    backend = GlueIcebergBackend(
        database="testdb",
        table="evidence",
        s3_warehouse="s3://test-bucket/warehouse",
        region="us-east-1",
    )
    fake = _FakeIcebergTable()
    monkeypatch.setattr(backend, "_get_table", lambda: fake)

    r = BitemporalRecord.new(
        key="USDC/USD",
        valid_time=datetime(2026, 4, 17, tzinfo=UTC),
        value={"px": 1.0001},
        source="test",
        source_trust_level="primary",
    )
    backend.append(r)

    hist = backend.history("USDC/USD")
    assert [x.record_id for x in hist] == [r.record_id]
    assert hist[0].value == {"px": 1.0001}
    assert backend.history("other") == []
