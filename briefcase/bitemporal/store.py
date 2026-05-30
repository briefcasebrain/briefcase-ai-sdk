"""
BitemporalStore — the append-only store protocol.

There is no ``update`` method by design. In a bitemporal model, every change
to knowledge is additive. Correcting a previously published value means
appending a new record with the same ``valid_time`` and a new
``transaction_time`` — see :func:`briefcase.bitemporal.append_correction`.

This module ships an in-memory reference implementation suitable for tests,
examples, and modest production use. Swap in a SQLite / kdb+ / Iceberg
backend by implementing the :class:`BitemporalStore` protocol; the
:class:`~briefcase.bitemporal.AsOfView` wrapper and replay code are
backend-agnostic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, List, Optional, Protocol, runtime_checkable

from briefcase.bitemporal.record import BitemporalRecord


@runtime_checkable
class BitemporalStore(Protocol):
    """Append-only store of :class:`BitemporalRecord` rows.

    Implementations must satisfy:

    * ``append`` never mutates an existing record.
    * ``as_of`` returns the record whose ``transaction_time`` is the latest
      at or before the clamp, among records whose ``valid_time`` is at or
      before the clamp. This is the "what did we believe at time T" query.
    * ``history`` returns all records for a key in insertion order.
    * ``latest`` is equivalent to ``as_of`` with ``transaction_time=now``
      and no ``valid_time`` clamp.
    """

    def append(self, record: BitemporalRecord) -> None: ...

    def append_many(self, records: Iterable[BitemporalRecord]) -> None: ...

    def history(self, key: str) -> List[BitemporalRecord]: ...

    def latest(self, key: str) -> Optional[BitemporalRecord]: ...

    def as_of(
        self,
        key: str,
        *,
        transaction_time: Optional[datetime] = None,
        valid_time: Optional[datetime] = None,
    ) -> Optional[BitemporalRecord]: ...

    def keys(self) -> List[str]: ...


class InMemoryBitemporalStore:
    """Reference implementation. Thread-unsafe — wrap with a lock if needed."""

    def __init__(self) -> None:
        # key -> list of records in insertion order
        self._rows: Dict[str, List[BitemporalRecord]] = {}

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def append(self, record: BitemporalRecord) -> None:
        bucket = self._rows.setdefault(record.key, [])
        # Defensive: record_id must be unique even across keys.
        for existing in bucket:
            if existing.record_id == record.record_id:
                raise ValueError(
                    f"record_id {record.record_id!r} already present"
                )
        bucket.append(record)

    def append_many(self, records: Iterable[BitemporalRecord]) -> None:
        for r in records:
            self.append(r)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def history(self, key: str) -> List[BitemporalRecord]:
        return list(self._rows.get(key, ()))

    def latest(self, key: str) -> Optional[BitemporalRecord]:
        return self.as_of(key)

    def as_of(
        self,
        key: str,
        *,
        transaction_time: Optional[datetime] = None,
        valid_time: Optional[datetime] = None,
    ) -> Optional[BitemporalRecord]:
        bucket = self._rows.get(key)
        if not bucket:
            return None

        # Filter first by transaction_time clamp: the system can only know
        # facts it had learned by the clamp.
        candidates: List[BitemporalRecord] = []
        for r in bucket:
            if transaction_time is not None and r.transaction_time > transaction_time:
                continue
            if valid_time is not None and r.valid_time > valid_time:
                continue
            candidates.append(r)

        if not candidates:
            return None

        # Among surviving candidates pick the one with the latest
        # transaction_time. If tied, prefer the latest valid_time; if still
        # tied, prefer the record whose parent_record_id points to another
        # (i.e. the correction wins over the corrected).
        def sort_key(r: BitemporalRecord):
            return (
                r.transaction_time,
                r.valid_time,
                1 if r.parent_record_id else 0,
            )

        return max(candidates, key=sort_key)

    def keys(self) -> List[str]:
        return list(self._rows.keys())

    # Convenience helpers used by AsOfView and tests.
    def __len__(self) -> int:
        return sum(len(v) for v in self._rows.values())
