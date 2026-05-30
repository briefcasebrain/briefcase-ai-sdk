"""Core guardrail data types.

Layer-0 of the guardrail framework: small, dependency-free value types shared
by the protocols, wrappers, registry, and pipeline in :mod:`briefcase.guardrails.framework`.
Kept in their own module so the large framework module stays focused on
behavior rather than data definitions. Re-exported from ``framework`` and from
``briefcase.guardrails`` for backwards compatibility.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class Effect(str, Enum):
    """Decision outcome. Mirrors Cedar allow/deny but uses guardrail vocabulary."""
    ALLOW = "allow"
    DENY = "deny"


class ViolationMode(str, Enum):
    """What happens on deny."""
    BLOCK = "block"
    WARN = "warn"
    AUDIT = "audit"


@dataclass(frozen=True)
class EvalRequest:
    """A single evaluation request — the 'action' in Gymnasium terms.

    Frozen (immutable) so it can be hashed for caching and used as a dict key.
    """
    agent: str
    action: str
    resource: str
    context: Dict[str, Any] = field(default_factory=dict)
    request_id: Optional[str] = None

    def cache_key(self) -> str:
        """SHA-256 of the canonical request, used as a cache key."""
        serialized = json.dumps(
            {"a": self.agent, "c": self.action, "r": self.resource,
             "x": self.context},
            sort_keys=True, default=str,
        )
        return hashlib.sha256(serialized.encode()).hexdigest()


@dataclass
class EvalResult:
    """Evaluation output — the 'observation' in Gymnasium terms.

    Carries the decision, the provenance trail, and timing metadata.
    Mutable (not frozen) because wrappers may enrich it.
    """
    effect: Effect
    guardrail_name: str
    reason: Optional[str] = None
    policy_id: Optional[str] = None         # internal Cedar id — audit only
    lakefs_sha: Optional[str] = None        # commit SHA at decision time
    eval_time_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_allowed(self) -> bool:
        return self.effect == Effect.ALLOW


@dataclass
class Explanation:
    """Human-readable explanation of an EvalResult.

    Three formats: structured (for APIs), narrative (for clinicians),
    compliance (for auditors). Mirrors DecisionExplanation from v2 but
    decoupled from telemetry — produced directly by the env.
    """
    decision_id: Optional[str]
    effect: Effect
    guardrail_name: str
    extraction: Optional[Dict[str, Any]] = None  # attribute, value, quote
    policy_applied: Optional[Dict[str, Any]] = None  # name, version, condition
    rbac_result: Optional[Dict[str, Any]] = None
    abac_result: Optional[Dict[str, Any]] = None
    lakefs_sha: Optional[str] = None
    eval_time_ms: float = 0.0

    def to_narrative(self, locale: str = "en") -> str:
        """One-paragraph explanation for non-technical stakeholders."""
        parts = []
        if self.extraction and "quote" in self.extraction:
            parts.append(
                f'The AI found "{self.extraction["quote"]}" in the source document'
            )
            if "attribute" in self.extraction and "value" in self.extraction:
                parts.append(
                    f' and interpreted it as {self.extraction["attribute"]}'
                    f' = {self.extraction["value"]}.'
                )
        if self.policy_applied:
            name = self.policy_applied.get("name", "unknown")
            condition = self.policy_applied.get("condition", "")
            parts.append(
                f" Policy {name} requires {condition}."
            )
        parts.append(f" Result: {self.effect.value.upper()}.")
        return "".join(parts).strip()

    def to_compliance_json(self) -> Dict[str, Any]:
        """Full trace with all OTel-mappable attributes."""
        return {
            "decision_id": self.decision_id,
            "effect": self.effect.value,
            "guardrail_name": self.guardrail_name,
            "extraction": self.extraction,
            "policy_applied": self.policy_applied,
            "rbac_result": self.rbac_result,
            "abac_result": self.abac_result,
            "lakefs_sha": self.lakefs_sha,
            "eval_time_ms": self.eval_time_ms,
        }


__all__ = [
    "Effect",
    "ViolationMode",
    "EvalRequest",
    "EvalResult",
    "Explanation",
]
