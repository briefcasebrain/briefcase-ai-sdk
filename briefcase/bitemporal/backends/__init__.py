"""
Pluggable storage backends for :class:`briefcase.bitemporal.BitemporalStore`.

The SDK ships backends spanning a range of durability and scale tiers:

* **In-memory**: :class:`briefcase.bitemporal.InMemoryBitemporalStore`.
  Reference implementation, default for tests and examples.
* **SQLite**: :class:`SqliteBitemporalBackend`. Durable, embedded,
  suitable for single-node production. Append-only enforced via
  triggers.
* **Iceberg**: :class:`IcebergBitemporalBackend`. Wraps pyiceberg; runs
  against any supported catalog (SQLite, REST, Hive, Nessie, Glue).
* **Glue Iceberg**: :class:`GlueIcebergBackend`. Iceberg against an AWS
  Glue catalog with STS role assumption and a database pre-flight check.
* **kdb+**: :class:`KdbBitemporalBackend`. Tick-rate workloads via the
  commercial ``pykx`` client.

Backend modules import eagerly here; their heavy dependencies (pyiceberg,
boto3, pykx) load lazily on first use, so a bare install imports cleanly
and fails with a clear install hint when a backend is actually used.

All backends share the :class:`BitemporalStore` protocol so application
code and :class:`AsOfView` are backend-agnostic.
"""

from briefcase.bitemporal.backends.iceberg import IcebergBitemporalBackend
from briefcase.bitemporal.backends.iceberg_glue import GlueIcebergBackend
from briefcase.bitemporal.backends.kdb import KdbBitemporalBackend
from briefcase.bitemporal.backends.sqlite import SqliteBitemporalBackend

__all__ = [
    "GlueIcebergBackend",
    "IcebergBitemporalBackend",
    "KdbBitemporalBackend",
    "SqliteBitemporalBackend",
]
