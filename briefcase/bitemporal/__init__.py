"""
Bitemporal evidence primitives for auditable AI decisions.

Motivation
----------
When an AI agent makes a consequential decision — a compliance alert, a SAR
narrative, an agentic payment routing — an examiner may need to reconstruct
the decision months or years later. A single-timestamp store cannot answer
"what did the system know on the decision date", only "what does the system
know now". This module supplies the primitives that make reconstruction
possible by construction:

    valid_time       when a fact was true in the real world
    transaction_time when the system learned about that fact

Every record carries both. Writes are append-only; corrections are new
records that share a ``valid_time`` with a prior record but carry a fresh
``transaction_time`` and a ``parent_record_id``. Queries can be clamped to
a historical ``transaction_time`` through :class:`AsOfView`, so application
code does not change between production and backtest/replay.

Example
-------
    from briefcase.bitemporal import (
        BitemporalRecord, InMemoryBitemporalStore, AsOfView, append_correction,
    )

    store = InMemoryBitemporalStore()
    r = BitemporalRecord.new(
        key="USDC/USD", valid_time=t0, value=1.0001, source="bloomberg",
    )
    store.append(r)

    # Bloomberg corrects the value. Original belief preserved.
    append_correction(store, r, corrected_value=1.0002)

    # Examiner-replay: what did we believe on the decision date?
    with AsOfView(store, transaction_time=decision_ts) as view:
        assert view.latest("USDC/USD").value == 1.0001
"""

from briefcase.bitemporal.record import BitemporalRecord
from briefcase.bitemporal.store import (
    BitemporalStore,
    InMemoryBitemporalStore,
)
from briefcase.bitemporal.asof import AsOfView
from briefcase.bitemporal.corrections import append_correction
from briefcase.bitemporal.ingest import batch_append, stream_append

__all__ = [
    "BitemporalRecord",
    "BitemporalStore",
    "InMemoryBitemporalStore",
    "AsOfView",
    "append_correction",
    "batch_append",
    "stream_append",
]
