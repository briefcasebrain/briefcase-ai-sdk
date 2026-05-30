"""IcebergBitemporalBackend — round-trip against a local SqlCatalog."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

import pytest

pyiceberg = pytest.importorskip("pyiceberg")

from briefcase.bitemporal import (  # noqa: E402
    BitemporalRecord,
    BitemporalStore,
    append_correction,
)
from briefcase.bitemporal.backends import IcebergBitemporalBackend  # noqa: E402


UTC = timezone.utc


@pytest.fixture
def iceberg_backend():
    tmp = tempfile.TemporaryDirectory()
    catalog_db = os.path.join(tmp.name, "catalog.db")
    warehouse = os.path.join(tmp.name, "warehouse")
    os.makedirs(warehouse, exist_ok=True)
    backend = IcebergBitemporalBackend(
        catalog_name="briefcase_test",
        namespace="bc_test",
        table="evidence",
        warehouse=f"file://{warehouse}",
        catalog_uri=f"sqlite:///{catalog_db}",
        catalog_type="sql",
    )
    try:
        yield backend
    finally:
        tmp.cleanup()


def test_iceberg_satisfies_protocol(iceberg_backend):
    assert isinstance(iceberg_backend, BitemporalStore)


def test_iceberg_round_trip(iceberg_backend):
    r = BitemporalRecord.new(
        key="USDC/USD",
        valid_time=datetime(2026, 4, 17, tzinfo=UTC),
        value={"px": 1.0001, "size": 1_000_000},
        source="bloomberg",
        source_trust_level="primary",
    )
    iceberg_backend.append(r)
    loaded = iceberg_backend.latest("USDC/USD")
    assert loaded is not None
    assert loaded.record_id == r.record_id
    assert loaded.value == {"px": 1.0001, "size": 1_000_000}
    assert iceberg_backend.keys() == ["USDC/USD"]


def test_iceberg_correction_and_asof(iceberg_backend):
    original = BitemporalRecord.new(
        key="USDC/USD",
        valid_time=datetime(2026, 4, 17, tzinfo=UTC),
        value={"px": 1.0001},
        source="bloomberg",
        transaction_time=datetime(2026, 4, 17, tzinfo=UTC),
    )
    iceberg_backend.append(original)
    append_correction(
        iceberg_backend,
        original,
        corrected_value={"px": 1.0002},
        transaction_time=datetime(2026, 5, 17, tzinfo=UTC),
    )

    # Two rows total.
    assert len(iceberg_backend.history("USDC/USD")) == 2

    early = iceberg_backend.as_of(
        "USDC/USD", transaction_time=datetime(2026, 5, 1, tzinfo=UTC)
    )
    assert early.value == {"px": 1.0001}

    late = iceberg_backend.as_of(
        "USDC/USD", transaction_time=datetime(2026, 6, 1, tzinfo=UTC)
    )
    assert late.value == {"px": 1.0002}
    assert late.parent_record_id == original.record_id


def test_iceberg_empty_table_queries_are_safe(iceberg_backend):
    # history/latest/keys must not crash on an uninitialized table.
    assert iceberg_backend.history("missing") == []
    assert iceberg_backend.latest("missing") is None
    assert iceberg_backend.keys() == []
