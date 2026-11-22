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

Backends by name: :func:`get_backend` (``"memory"``, ``"sqlite"``,
``"iceberg"``, ``"glue"``, ``"kdb"``, or a backend another package registers
under the ``briefcase.bitemporal_backends`` entry point group; see
:mod:`briefcase.plugins`). :func:`available_backends` lists the names.
"""

from typing import Any, Dict, List

from briefcase.bitemporal.backends.iceberg import IcebergBitemporalBackend
from briefcase.bitemporal.backends.iceberg_glue import GlueIcebergBackend
from briefcase.bitemporal.backends.kdb import KdbBitemporalBackend
from briefcase.bitemporal.backends.sqlite import SqliteBitemporalBackend

#: Built-in backends by name. A plugin cannot take one of these names.
BUILTIN_BACKENDS: Dict[str, str] = {
    "memory": "briefcase.bitemporal.store:InMemoryBitemporalStore",
    "sqlite": "briefcase.bitemporal.backends.sqlite:SqliteBitemporalBackend",
    "iceberg": "briefcase.bitemporal.backends.iceberg:IcebergBitemporalBackend",
    "glue": "briefcase.bitemporal.backends.iceberg_glue:GlueIcebergBackend",
    "kdb": "briefcase.bitemporal.backends.kdb:KdbBitemporalBackend",
}
_GROUP = "briefcase.bitemporal_backends"


def get_backend(name: str) -> Any:
    """The backend class registered as ``name``: a built-in, else a plugin.

    Raises KeyError naming the available backends (and, for a plugin that is
    installed but could not be loaded, why)."""
    import importlib

    if name in BUILTIN_BACKENDS:
        module_name, _, attr = BUILTIN_BACKENDS[name].partition(":")
        return getattr(importlib.import_module(module_name), attr)
    from briefcase import plugins

    cls = plugins.get(_GROUP, name, BUILTIN_BACKENDS)
    if cls is None:
        detail = plugins.explain(_GROUP, name)
        raise KeyError(f"no bitemporal backend {name!r} ({detail}); available: "
                       f"{', '.join(available_backends())}")
    return cls


def open_backend(name: str, *args: Any, **kwargs: Any) -> Any:
    """``get_backend(name)(*args, **kwargs)``."""
    return get_backend(name)(*args, **kwargs)


def available_backends() -> List[str]:
    """Built-in names first, then installed plugin names (not imported)."""
    from briefcase import plugins

    extra = [n for n in plugins.names(_GROUP) if n not in BUILTIN_BACKENDS]
    return list(BUILTIN_BACKENDS) + extra


__all__ = [
    "BUILTIN_BACKENDS",
    "available_backends",
    "get_backend",
    "open_backend",
    "GlueIcebergBackend",
    "IcebergBitemporalBackend",
    "KdbBitemporalBackend",
    "SqliteBitemporalBackend",
]
