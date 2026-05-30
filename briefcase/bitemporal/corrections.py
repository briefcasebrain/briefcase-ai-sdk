"""
Append-only corrections.

In a bitemporal model the correct response to an upstream correction is
NOT to overwrite the original record. It is to append a new record that
shares the same ``valid_time`` as the original but carries a fresh
``transaction_time`` and a ``parent_record_id`` pointing to the original.

The history of beliefs is preserved. Examiner replay answers "what did we
believe on the decision date" by clamping ``transaction_time`` to the
decision date; the original pre-correction record wins because the
correction had not yet been learned.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from briefcase.bitemporal.record import BitemporalRecord
from briefcase.bitemporal.store import BitemporalStore


def append_correction(
    store: BitemporalStore,
    original: BitemporalRecord,
    corrected_value: Any,
    *,
    source: Optional[str] = None,
    transaction_time: Optional[datetime] = None,
    metadata: Optional[dict] = None,
    decision: Optional[str] = None,
) -> BitemporalRecord:
    """Append a correction for an existing record.

    The new record inherits ``key`` and ``valid_time`` from ``original``
    and sets ``parent_record_id = original.record_id``. ``transaction_time``
    defaults to now; override only when replaying historical corrections.

    Returns the new record.

    Raises
    ------
    ValueError
        If the supplied ``transaction_time`` is not strictly after
        ``original.transaction_time``. A correction that is not newer than
        what it corrects violates the invariant that corrections supersede.
    """
    new_metadata = dict(original.metadata)
    new_metadata.update(metadata or {})
    new_metadata.setdefault("correction_of", original.record_id)

    correction = BitemporalRecord.new(
        key=original.key,
        valid_time=original.valid_time,
        value=corrected_value,
        source=source or original.source,
        transaction_time=transaction_time,
        decision=decision,
        source_trust_level=original.source_trust_level,
        parent_record_id=original.record_id,
        metadata=new_metadata,
    )

    if correction.transaction_time <= original.transaction_time:
        raise ValueError(
            "correction.transaction_time must be strictly after the "
            "original's transaction_time"
        )

    store.append(correction)
    return correction
