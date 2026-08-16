"""
BitemporalRecord — the canonical five-field record.

The schema is the one described in the Steve Cannon notes, unfolded so that
``timestamp`` splits into its two axes:

    { valid_time, transaction_time, value, decision, source }

plus optional fields that make the record useful for governance:

    source_trust_level   per-source policy input (e.g. "primary", "derived")
    parent_record_id     set when this record is a correction of another
    metadata             freeform attribution (api endpoint, feed version, ...)

Records are immutable by convention. The store never mutates them.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from briefcase.integrity.canonical import canonical_json_compat


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(value: Any) -> str:
    """Canonical JSON encoding used for hashing and equality.

    The compat profile: byte-compatible with hashes stored by earlier
    releases, so ``content_hash`` values never change.
    """
    return canonical_json_compat(value)


@dataclass(frozen=True)
class BitemporalRecord:
    """
    Immutable bitemporal record.

    Attributes
    ----------
    record_id
        UUID assigned at append time. Stable identity for the row.
    key
        Logical identity of the fact (e.g. "USDC/USD", "ofac_sdn:12345").
        Two records with the same ``key`` describe beliefs about the same
        real-world entity at (potentially) different times.
    valid_time
        When the fact was true in the real world.
    transaction_time
        When the store learned about the fact.
    value
        The observed value. Any JSON-serializable payload.
    source
        Attribution string. Required because corrections are always
        associated back to a specific upstream provider.
    decision
        Optional identifier of the decision this record informed. Binds
        evidence to action so the audit trail answers "what did we know
        and what did we do about it", not just "what did we know".
    source_trust_level
        Optional policy hint (e.g. "primary", "derived", "unverified").
    parent_record_id
        Set when this record corrects an earlier one. The two records share
        ``valid_time``; the later ``transaction_time`` supersedes the
        earlier belief.
    metadata
        Freeform attribution dict. Not part of the equality contract.
    """

    record_id: str
    key: str
    valid_time: datetime
    transaction_time: datetime
    value: Any
    source: Optional[str]
    decision: Optional[str] = None
    source_trust_level: Optional[str] = None
    parent_record_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def new(
        cls,
        key: str,
        valid_time: datetime,
        value: Any,
        source: Optional[str],
        *,
        transaction_time: Optional[datetime] = None,
        decision: Optional[str] = None,
        source_trust_level: Optional[str] = None,
        parent_record_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        record_id: Optional[str] = None,
    ) -> "BitemporalRecord":
        """Build a record, supplying sensible defaults."""
        if valid_time.tzinfo is None:
            raise ValueError("valid_time must be timezone-aware")
        tx = transaction_time or _utcnow()
        if tx.tzinfo is None:
            raise ValueError("transaction_time must be timezone-aware")
        return cls(
            record_id=record_id or str(uuid.uuid4()),
            key=key,
            valid_time=valid_time,
            transaction_time=tx,
            value=value,
            source=source,
            decision=decision,
            source_trust_level=source_trust_level,
            parent_record_id=parent_record_id,
            metadata=dict(metadata or {}),
        )

    # ------------------------------------------------------------------
    # Derived fields
    # ------------------------------------------------------------------

    def content_hash(self) -> str:
        """SHA-256 of the canonical value payload. Useful for dedup/audit."""
        return hashlib.sha256(_canonical_json(self.value).encode()).hexdigest()

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["valid_time"] = self.valid_time.isoformat()
        d["transaction_time"] = self.transaction_time.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BitemporalRecord":
        return cls(
            record_id=d["record_id"],
            key=d["key"],
            valid_time=datetime.fromisoformat(d["valid_time"]),
            transaction_time=datetime.fromisoformat(d["transaction_time"]),
            value=d["value"],
            source=d["source"],
            decision=d.get("decision"),
            source_trust_level=d.get("source_trust_level"),
            parent_record_id=d.get("parent_record_id"),
            metadata=dict(d.get("metadata") or {}),
        )
