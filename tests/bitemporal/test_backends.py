"""Backend stub (kdb+) protocol conformance + fail-loud semantics.

The Iceberg backend is now a real implementation — its tests live in
``test_iceberg_backend.py``. The SQLite backend's tests live in
``test_sqlite_backend.py``. Only kdb+ is a stub.
"""

from datetime import datetime, timezone

import pytest

from briefcase.bitemporal import BitemporalRecord, BitemporalStore
from briefcase.bitemporal.backends import KdbBitemporalBackend


UTC = timezone.utc


def _record() -> BitemporalRecord:
    return BitemporalRecord.new(
        key="k",
        valid_time=datetime(2026, 4, 17, tzinfo=UTC),
        value=1,
        source="test",
    )


def test_kdb_stub_satisfies_bitemporal_store_protocol():
    assert isinstance(KdbBitemporalBackend(), BitemporalStore)


def test_kdb_stub_fails_loudly_on_every_operation():
    instance = KdbBitemporalBackend()
    r = _record()
    with pytest.raises(NotImplementedError):
        instance.append(r)
    with pytest.raises(NotImplementedError):
        instance.append_many([r])
    with pytest.raises(NotImplementedError):
        instance.history("k")
    with pytest.raises(NotImplementedError):
        instance.latest("k")
    with pytest.raises(NotImplementedError):
        instance.as_of("k", transaction_time=datetime(2026, 4, 17, tzinfo=UTC))
    with pytest.raises(NotImplementedError):
        instance.keys()


def test_kdb_backend_keeps_construction_kwargs():
    b = KdbBitemporalBackend(host="kdb.internal", port=5001, namespace=".bc2")
    assert b.host == "kdb.internal"
    assert b.port == 5001
    assert b.namespace == ".bc2"
