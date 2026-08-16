"""Backend surface checks.

Each backend's behavior is covered in its own module
(``test_sqlite_backend.py``, ``test_iceberg_backend.py``,
``test_kdb_backend.py``). This module checks the package-level exports
and lazy-construction contract shared by all backends.
"""

from briefcase.bitemporal import BitemporalStore
from briefcase.bitemporal.backends import (
    IcebergBitemporalBackend,
    KdbBitemporalBackend,
)


def test_kdb_backend_satisfies_bitemporal_store_protocol():
    assert isinstance(KdbBitemporalBackend(), BitemporalStore)


def test_kdb_backend_keeps_construction_kwargs():
    b = KdbBitemporalBackend(host="kdb.internal", port=5001, table="evidence2")
    assert b.host == "kdb.internal"
    assert b.port == 5001
    assert b.table == "evidence2"


def test_kdb_backend_construction_does_not_connect():
    b = KdbBitemporalBackend(host="unreachable.invalid", port=1)
    assert b._conn_obj is None


def test_iceberg_close_drops_cached_refs_and_calls_catalog_close():
    # Construction is lazy, so no pyiceberg install is needed here.
    b = IcebergBitemporalBackend()
    closed = {"flag": False}

    class _Catalog:
        def close(self):
            closed["flag"] = True

    b._catalog = _Catalog()
    b._table = object()
    b.close()
    assert closed["flag"]
    assert b._catalog is None
    assert b._table is None


def test_iceberg_close_is_safe_when_never_opened():
    b = IcebergBitemporalBackend()
    b.close()
    assert b._catalog is None
    assert b._table is None
