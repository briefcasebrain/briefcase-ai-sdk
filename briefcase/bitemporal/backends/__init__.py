"""
Pluggable storage backends for :class:`briefcase.bitemporal.BitemporalStore`.

The OSS SDK ships three backends spanning the tiers from the design
notes:

* **In-memory** — :class:`briefcase.bitemporal.InMemoryBitemporalStore`.
  Reference implementation, default for tests and examples.
* **SQLite** — :class:`SqliteBitemporalBackend`. Durable, embedded,
  suitable for single-node production. Append-only enforced via
  triggers.
* **Iceberg** — :class:`IcebergBitemporalBackend`. Wraps pyiceberg; runs
  against any supported catalog (SQLite, REST, Hive, Nessie, Glue).

A kdb+ stub (:class:`KdbBitemporalBackend`) declares the protocol surface
but raises :class:`NotImplementedError` on every call — the production
implementation ships in ``briefcase-ai-sdk-enterprise`` because the
``pykx`` client is commercial.

All backends share the :class:`BitemporalStore` protocol so application
code and :class:`AsOfView` are backend-agnostic.
"""

from briefcase.bitemporal.backends.iceberg import IcebergBitemporalBackend
from briefcase.bitemporal.backends.kdb import KdbBitemporalBackend
from briefcase.bitemporal.backends.sqlite import SqliteBitemporalBackend

__all__ = [
    "IcebergBitemporalBackend",
    "KdbBitemporalBackend",
    "SqliteBitemporalBackend",
]
