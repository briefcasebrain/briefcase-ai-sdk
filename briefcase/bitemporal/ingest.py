"""
Batch vs. instant ingestion helpers.

Both patterns produce the same bitemporal output; the difference is the
latency budget and the ``transaction_time`` semantics:

    batch_append    one shared ``transaction_time`` for the whole batch.
                    Models the end-of-day file that "settles" into the
                    historical record at a single instant.

    stream_append   per-event ``transaction_time`` assigned at append.
                    Models the tick / event stream where each record is
                    learned independently.

Application code that reads through :class:`~briefcase.bitemporal.AsOfView`
cannot distinguish between the two. That equivalence is a test invariant
and should be preserved by any backend implementing :class:`BitemporalStore`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from briefcase.bitemporal.record import BitemporalRecord
from briefcase.bitemporal.store import BitemporalStore


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def batch_append(
    store: BitemporalStore,
    records: Iterable[BitemporalRecord],
    *,
    transaction_time: Optional[datetime] = None,
) -> List[BitemporalRecord]:
    """Append many records with a single shared ``transaction_time``.

    If ``transaction_time`` is not supplied it defaults to now. Records
    that already carry a ``transaction_time`` are rewritten to use the
    shared one so that the batch settles as a single instant in the
    transaction-time axis.
    """
    tx = transaction_time or _utcnow()
    if tx.tzinfo is None:
        raise ValueError("transaction_time must be timezone-aware")

    normalized: List[BitemporalRecord] = []
    for r in records:
        # BitemporalRecord is frozen; dataclasses.replace returns a new one.
        normalized.append(replace(r, transaction_time=tx))

    store.append_many(normalized)
    return normalized


def stream_append(
    store: BitemporalStore,
    record: BitemporalRecord,
) -> BitemporalRecord:
    """Append a single record with ``transaction_time`` set to now.

    If the record already has a ``transaction_time`` it is preserved — this
    allows streaming pipelines that have their own clock source (e.g. the
    broker's event timestamp) to pass that timestamp through.
    """
    if record.transaction_time is None:
        record = replace(record, transaction_time=_utcnow())
    store.append(record)
    return record
