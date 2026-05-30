"""
kdb+ adapter for :class:`BitemporalStore`.

kdb+ is the natural fit for tick-rate workloads: columnar, time-series
native, and with bitemporal semantics built into the engine rather than
bolted on. This adapter declares the protocol surface; the production
implementation ships in ``briefcase-ai-sdk-enterprise`` because the
``kx`` and ``pykx`` client libraries are commercial.

Design notes for the enterprise implementation
----------------------------------------------
* Map ``BitemporalRecord`` rows onto a keyed table partitioned by date.
* ``append`` → ``.Q.en`` + ``upsert`` with ``transaction_time`` and
  ``valid_time`` as indexed columns.
* ``as_of`` → q function that filters on both axes and takes the latest
  ``transaction_time`` per key. Evaluate with ``pykx.q`` so the native
  engine does the work rather than Python.
* ``history`` → ``select from table where key=X`` ordered by
  ``transaction_time``.

Open issues
-----------
* Schema evolution: ``value`` is arbitrary JSON here but kdb+ prefers
  typed columns. Enterprise implementation will project ``value`` into
  per-source tables.
* Replication and backup semantics differ from the SQLite backend; the
  enterprise adapter will document the tradeoffs in its README.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, List, Optional

from briefcase.bitemporal.record import BitemporalRecord


_NOT_IMPLEMENTED = (
    "KdbBitemporalBackend is a protocol stub in the OSS SDK. "
    "The production implementation ships in briefcase-ai-sdk-enterprise "
    "(https://github.com/briefcasebrain/briefcase-ai-sdk-enterprise). "
    "Install it and configure a kdb+ connection to enable this backend."
)


class KdbBitemporalBackend:
    """Protocol-only stub for a kdb+-backed bitemporal store.

    Construction does not require a connection; all operations raise
    :class:`NotImplementedError` so misconfiguration fails loudly.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5000,
        *,
        namespace: str = ".bc",
        table: str = "evidence",
        **client_kwargs: Any,
    ) -> None:
        self.host = host
        self.port = port
        self.namespace = namespace
        self.table = table
        self._client_kwargs = dict(client_kwargs)

    # ------------------------------------------------------------------
    # Protocol surface — BitemporalStore
    # ------------------------------------------------------------------

    def append(self, record: BitemporalRecord) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def append_many(self, records: Iterable[BitemporalRecord]) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def history(self, key: str) -> List[BitemporalRecord]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def latest(self, key: str) -> Optional[BitemporalRecord]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def as_of(
        self,
        key: str,
        *,
        transaction_time: Optional[datetime] = None,
        valid_time: Optional[datetime] = None,
    ) -> Optional[BitemporalRecord]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def keys(self) -> List[str]:
        raise NotImplementedError(_NOT_IMPLEMENTED)
