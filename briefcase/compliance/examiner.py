"""
ExaminerBundle — a reproducible, content-addressable audit artifact.

Given an :class:`AgentRoutingDecision`, a :class:`BitemporalStore`, and a
:class:`PolicyRegistry`, this module produces a self-contained JSON bundle
that reproduces the decision from first principles:

    { "decision": <AgentRoutingDecision>,
      "policy":   <PolicyVersion effective at decision_ts>,
      "evidence": [<BitemporalRecord>, ...],
      "integrity": { "content_hash": "sha256:...", "as_of_transaction_time": "..." } }

The bundle's ``content_hash`` is a SHA-256 of the canonical JSON of the
decision, policy, and evidence. Any downstream party can rebuild the hash
from the bundle contents and verify that no field has been tampered with.

The integrity guarantee is intentionally scoped: the hash proves the bundle
is internally consistent. Proving the bundle reflects what the production
system actually did requires storing the bundle alongside an independently
signed commit / WORM record — outside the scope of this module.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from briefcase.bitemporal.record import BitemporalRecord
from briefcase.bitemporal.store import BitemporalStore
from briefcase.routing.policy import AgentRoutingDecision, PolicyRegistry


class BundleIntegrityError(ValueError):
    """Raised when a bundle fails integrity verification."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _record_to_dict(record: BitemporalRecord) -> Dict[str, Any]:
    return record.to_dict()


@dataclass
class ExaminerBundle:
    """Reproducible audit artifact for a single agentic decision.

    Build with :meth:`build`. Export with :meth:`to_json`. Verify a bundle
    received from the outside with :meth:`verify`.
    """

    decision: Dict[str, Any]
    policy: Optional[Dict[str, Any]]
    evidence: List[Dict[str, Any]]
    as_of_transaction_time: Optional[str] = None
    content_hash: str = ""
    schema_version: str = "1"
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def build(
        cls,
        decision: AgentRoutingDecision,
        evidence_store: BitemporalStore,
        policy_registry: PolicyRegistry,
        *,
        as_of_transaction_time: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "ExaminerBundle":
        """Assemble a bundle from a decision and its supporting stores.

        Parameters
        ----------
        decision
            The :class:`AgentRoutingDecision` to reconstruct.
        evidence_store
            The bitemporal store containing the evidence records referenced
            in ``decision.evidence_refs``. Records are looked up by
            ``record_id`` across all keys.
        policy_registry
            Registry used to reload the policy version that was in effect
            at ``as_of_transaction_time``.
        as_of_transaction_time
            Clamp for policy lookup. Defaults to ``decision.decided_at`` so
            the bundle reconstructs the policy as-of the decision.
        metadata
            Freeform metadata attached to the bundle.
        """
        clamp = as_of_transaction_time or decision.decided_at

        # Policy as-of the decision.
        policy = policy_registry.get(
            decision.policy_id, as_of_transaction_time=clamp
        )
        policy_dict = policy.to_dict() if policy is not None else None

        # Evidence records. Lookup is O(N) across keys but bundles are small.
        wanted = set(decision.evidence_refs)
        evidence: List[Dict[str, Any]] = []
        if wanted:
            for key in evidence_store.keys():
                for r in evidence_store.history(key):
                    if r.record_id in wanted:
                        evidence.append(_record_to_dict(r))
                        wanted.discard(r.record_id)
            if wanted:
                raise BundleIntegrityError(
                    f"evidence refs not found in store: {sorted(wanted)}"
                )

        # Sort evidence deterministically for a stable hash.
        evidence.sort(key=lambda d: d["record_id"])

        bundle = cls(
            decision=decision.to_dict(),
            policy=policy_dict,
            evidence=evidence,
            as_of_transaction_time=clamp.isoformat(),
            metadata=dict(metadata or {}),
        )
        bundle.content_hash = bundle._compute_hash()
        return bundle

    # ------------------------------------------------------------------
    # Integrity
    # ------------------------------------------------------------------

    def _hashable_payload(self) -> Dict[str, Any]:
        """The subset of fields covered by ``content_hash``."""
        return {
            "schema_version": self.schema_version,
            "decision": self.decision,
            "policy": self.policy,
            "evidence": self.evidence,
            "as_of_transaction_time": self.as_of_transaction_time,
        }

    def _compute_hash(self) -> str:
        payload = _canonical_json(self._hashable_payload())
        return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()

    def verify(self) -> None:
        """Raise :class:`BundleIntegrityError` if the bundle is inconsistent."""
        expected = self._compute_hash()
        if expected != self.content_hash:
            raise BundleIntegrityError(
                f"content hash mismatch: expected {expected}, got {self.content_hash}"
            )

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision": self.decision,
            "policy": self.policy,
            "evidence": self.evidence,
            "as_of_transaction_time": self.as_of_transaction_time,
            "content_hash": self.content_hash,
            "metadata": self.metadata,
        }

    def to_json(self, *, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=indent, default=str)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExaminerBundle":
        return cls(
            decision=d["decision"],
            policy=d.get("policy"),
            evidence=list(d.get("evidence") or []),
            as_of_transaction_time=d.get("as_of_transaction_time"),
            content_hash=d.get("content_hash", ""),
            schema_version=d.get("schema_version", "1"),
            metadata=dict(d.get("metadata") or {}),
        )

    @classmethod
    def from_json(cls, s: str) -> "ExaminerBundle":
        return cls.from_dict(json.loads(s))
