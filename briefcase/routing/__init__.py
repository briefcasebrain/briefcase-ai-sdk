"""
Briefcase routing.

Two layers live here:

* :class:`BaseRouter` / :class:`RoutingDecision` — the original, narrow
  auto-vs-human-review router. Preserved unchanged for backwards
  compatibility.
* :class:`AgentRouter`, :class:`PolicyRegistry`, :class:`PolicyVersion`,
  :class:`PolicyRule`, :class:`AgentRoutingDecision` — the versioned,
  bitemporally-auditable routing layer used for agentic decisions where
  an examiner may later ask "which rule fired, which version was active".
"""

from briefcase.routing.base import BaseRouter, RoutingDecision
from briefcase.routing.policy import (
    AgentRouter,
    AgentRoutingDecision,
    PolicyEvaluationResult,
    PolicyRegistry,
    PolicyRule,
    PolicyVersion,
)

__all__ = [
    "BaseRouter",
    "RoutingDecision",
    "AgentRouter",
    "AgentRoutingDecision",
    "PolicyEvaluationResult",
    "PolicyRegistry",
    "PolicyRule",
    "PolicyVersion",
]
