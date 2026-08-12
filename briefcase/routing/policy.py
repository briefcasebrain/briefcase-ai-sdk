"""
Versioned routing policy — the "if this use case, then this stablecoin" layer.

Motivation
----------
When an AI agent executes on behalf of a user — routing a cross-border
payment, selecting a stablecoin issuer, picking a compliance-review path —
the routing choice is governed by a policy. The policy changes over time.
An examiner asking "why did this agent pick Circle over Tether on April 17"
needs to know (a) the full policy that was in effect on that date and
(b) the specific rule that fired.

Single-version policy stores cannot answer (a) once the policy has changed.
A versioned store backed by :mod:`briefcase.bitemporal` can, by
construction: the examiner clamps ``transaction_time`` to the decision
date and reads the rule set that was active then.

Data model
----------
    PolicyRule       a single "if condition matches, select choice" rule.
    PolicyVersion    an ordered list of rules effective between two dates.
    PolicyRegistry   bitemporal store of versions, keyed by policy_id.
    AgentRoutingDecision  the decision record an agent produces.

The registry is a thin facade over :class:`BitemporalStore`. Every version
bump is an append. The "as-of" query returns the policy that was in
effect at a given transaction_time — which is exactly what replay needs.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from briefcase.bitemporal.record import BitemporalRecord
from briefcase.bitemporal.store import BitemporalStore, InMemoryBitemporalStore


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Rule / version model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PolicyRule:
    """A single routing rule.

    ``condition`` is a small dict predicate evaluated against the routing
    context (see :meth:`PolicyVersion.select`). The simple form supports
    equality and membership; richer predicates can be added by a host
    application without changing the wire format.
    """

    rule_id: str
    condition: Dict[str, Any]
    choice: str
    rationale: Optional[str] = None

    def matches(self, context: Dict[str, Any]) -> bool:
        """Return True if the context satisfies the condition.

        Supported condition syntax:

            {"field": value}            equality
            {"field": {"in": [a, b]}}   membership
            {"field": {"ne": value}}    inequality

        Unknown operators raise KeyError so misconfiguration is loud.
        """
        for field_name, expected in self.condition.items():
            actual = context.get(field_name)
            if isinstance(expected, dict):
                if "in" in expected:
                    if actual not in expected["in"]:
                        return False
                elif "ne" in expected:
                    if actual == expected["ne"]:
                        return False
                else:
                    raise KeyError(
                        f"unsupported operator in condition: {expected!r}"
                    )
            else:
                if actual != expected:
                    return False
        return True


@dataclass(frozen=True)
class PolicyVersion:
    """A versioned, ordered list of rules.

    Rules are evaluated in order; the first match wins. If no rule matches
    and ``default_choice`` is set, it is returned; otherwise
    :meth:`select` returns ``None`` so the caller can decide (e.g. fall
    back to human review).
    """

    policy_id: str
    version: str
    rules: List[PolicyRule]
    default_choice: Optional[str] = None
    description: Optional[str] = None

    def select(
        self, context: Dict[str, Any]
    ) -> "PolicyEvaluationResult":
        for rule in self.rules:
            if rule.matches(context):
                return PolicyEvaluationResult(
                    choice=rule.choice,
                    matched_rule_id=rule.rule_id,
                    policy_id=self.policy_id,
                    policy_version=self.version,
                    rationale=rule.rationale,
                )
        return PolicyEvaluationResult(
            choice=self.default_choice,
            matched_rule_id=None,
            policy_id=self.policy_id,
            policy_version=self.version,
            rationale="default_choice" if self.default_choice else "no_match",
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "rules": [asdict(r) for r in self.rules],
            "default_choice": self.default_choice,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PolicyVersion":
        return cls(
            policy_id=d["policy_id"],
            version=d["version"],
            rules=[PolicyRule(**r) for r in d["rules"]],
            default_choice=d.get("default_choice"),
            description=d.get("description"),
        )


@dataclass(frozen=True)
class PolicyEvaluationResult:
    """Outcome of evaluating a policy against a context."""
    choice: Optional[str]
    matched_rule_id: Optional[str]
    policy_id: str
    policy_version: str
    rationale: Optional[str] = None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class PolicyRegistry:
    """Bitemporal registry of routing policies.

    Each policy is stored under a key of the form ``policy:<policy_id>``.
    Publishing a new version is an append — no mutation. Reading "the
    policy as of date X" is a clamped as-of query, which is how examiner
    replay reconstructs the rule set that was active on the decision date.
    """

    _KEY_PREFIX = "policy:"

    def __init__(self, store: Optional[BitemporalStore] = None) -> None:
        self._store = store or InMemoryBitemporalStore()

    @property
    def store(self) -> BitemporalStore:
        return self._store

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def publish(
        self,
        policy: PolicyVersion,
        *,
        valid_from: datetime,
        transaction_time: Optional[datetime] = None,
        source: str = "policy_registry",
    ) -> BitemporalRecord:
        """Publish a new policy version effective from ``valid_from``.

        ``valid_from`` maps to ``BitemporalRecord.valid_time`` — when the
        policy was effective in the real world. ``transaction_time``
        defaults to now and records when the registry learned of the
        version.
        """
        record = BitemporalRecord.new(
            key=f"{self._KEY_PREFIX}{policy.policy_id}",
            valid_time=valid_from,
            value=policy.to_dict(),
            source=source,
            transaction_time=transaction_time,
            metadata={"policy_version": policy.version},
        )
        self._store.append(record)
        return record

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(
        self,
        policy_id: str,
        *,
        as_of_transaction_time: Optional[datetime] = None,
        as_of_valid_time: Optional[datetime] = None,
    ) -> Optional[PolicyVersion]:
        """Return the policy version visible at the given as-of point.

        If both clamps are omitted, returns the latest published version.
        """
        record = self._store.as_of(
            f"{self._KEY_PREFIX}{policy_id}",
            transaction_time=as_of_transaction_time,
            valid_time=as_of_valid_time,
        )
        if record is None:
            return None
        return PolicyVersion.from_dict(record.value)

    def history(self, policy_id: str) -> List[PolicyVersion]:
        records = self._store.history(f"{self._KEY_PREFIX}{policy_id}")
        return [PolicyVersion.from_dict(r.value) for r in records]


# ---------------------------------------------------------------------------
# Agent routing decision record
# ---------------------------------------------------------------------------

@dataclass
class AgentRoutingDecision:
    """Record of an agentic routing decision.

    Joins together the three things an examiner needs to reconstruct the
    choice: the declared use case, the evidence considered, and the policy
    rule that fired. Serialize with :meth:`to_dict` or attach to the
    current Briefcase decision snapshot via tags.

    Attributes
    ----------
    decision_id
        Stable identifier for this routing decision.
    use_case
        Declared purpose (e.g. "cross_border_payout", "sar_narrative").
    context
        Input context the policy was evaluated against.
    candidates
        The set of choices the policy could have selected from.
    selected
        The actual choice that was made.
    policy_id / policy_version / matched_rule_id
        Attribution back to the versioned policy registry.
    evidence_refs
        Record IDs of :class:`BitemporalRecord` rows that informed the
        decision. These are the rows an examiner replays.
    rationale
        Optional human-readable explanation; usually copied from the
        matched rule.
    decided_at
        ISO-8601 decided timestamp. Defaults to now.
    """

    decision_id: str
    use_case: str
    context: Dict[str, Any]
    candidates: List[str]
    selected: Optional[str]
    policy_id: str
    policy_version: str
    matched_rule_id: Optional[str]
    evidence_refs: List[str] = field(default_factory=list)
    rationale: Optional[str] = None
    decided_at: datetime = field(default_factory=_utcnow)

    @classmethod
    def from_evaluation(
        cls,
        *,
        use_case: str,
        context: Dict[str, Any],
        candidates: List[str],
        evaluation: PolicyEvaluationResult,
        evidence_refs: Optional[List[str]] = None,
        decision_id: Optional[str] = None,
        decided_at: Optional[datetime] = None,
    ) -> "AgentRoutingDecision":
        return cls(
            decision_id=decision_id or str(uuid.uuid4()),
            use_case=use_case,
            context=dict(context),
            candidates=list(candidates),
            selected=evaluation.choice,
            policy_id=evaluation.policy_id,
            policy_version=evaluation.policy_version,
            matched_rule_id=evaluation.matched_rule_id,
            evidence_refs=list(evidence_refs or []),
            rationale=evaluation.rationale,
            decided_at=decided_at or _utcnow(),
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["decided_at"] = self.decided_at.isoformat()
        return d


# ---------------------------------------------------------------------------
# Agent router
# ---------------------------------------------------------------------------

class AgentRouter:
    """Convenience router that joins a registry and a context builder.

    The registry supplies the policy. The caller supplies the context.
    Returns an :class:`AgentRoutingDecision` ready to attach to a
    Briefcase decision snapshot.

    For examiner replay, pass ``as_of_transaction_time`` so the router
    uses the policy that was in effect on the decision date rather than
    the current one.

    Note
    ----
    :meth:`route` is intentionally **synchronous**: policy evaluation is a
    pure, in-memory computation against a bitemporal store. This differs from
    the I/O-bound, ``async`` :class:`briefcase.routing.BaseRouter` (auto-vs-human
    review). The two are independent abstractions, not an implementation of a
    shared interface, so the differing call conventions are by design.
    """

    def __init__(
        self,
        registry: PolicyRegistry,
        *,
        use_case: str,
        policy_id: str,
        candidates_provider: Optional[Callable[[Dict[str, Any]], List[str]]] = None,
    ) -> None:
        self._registry = registry
        self._use_case = use_case
        self._policy_id = policy_id
        self._candidates_provider = candidates_provider

    def route(
        self,
        context: Dict[str, Any],
        *,
        evidence_refs: Optional[List[str]] = None,
        as_of_transaction_time: Optional[datetime] = None,
        decided_at: Optional[datetime] = None,
    ) -> AgentRoutingDecision:
        """Evaluate the policy against ``context`` and record the decision.

        ``decided_at`` pins the decision timestamp; it defaults to now.
        Pin it for deterministic replay and tests, since downstream as-of
        reconstruction (e.g. ExaminerBundle) clamps to this value.
        """
        policy = self._registry.get(
            self._policy_id,
            as_of_transaction_time=as_of_transaction_time,
        )
        if policy is None:
            raise LookupError(
                f"no policy '{self._policy_id}' visible as-of "
                f"{as_of_transaction_time or 'now'}"
            )
        evaluation = policy.select(context)
        candidates = (
            self._candidates_provider(context)
            if self._candidates_provider
            else [r.choice for r in policy.rules]
        )
        return AgentRoutingDecision.from_evaluation(
            use_case=self._use_case,
            context=context,
            candidates=candidates,
            evaluation=evaluation,
            evidence_refs=evidence_refs,
            decided_at=decided_at,
        )
