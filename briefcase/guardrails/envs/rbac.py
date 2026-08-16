"""Role-based access control guardrail with glob resource matching."""

from __future__ import annotations

import fnmatch
import time
from typing import List, Optional

from briefcase.guardrails.framework import (
    BaseGuardrailEnv,
    Effect,
    EvalRequest,
    EvalResult,
    Explanation,
    PolicySpace,
)


class RBACGuardrailEnv(BaseGuardrailEnv):
    """Role-based access control guardrail.

    Returns ALLOW when the agent is permitted, the action is allowed, and
    the resource matches at least one allowed pattern (fnmatch glob).
    Returns DENY otherwise.
    """

    def __init__(
        self,
        agents: Optional[List[str]] = None,
        allowed_actions: Optional[List[str]] = None,
        allowed_resources: Optional[List[str]] = None,
        name: str = "rbac",
    ):
        agents = agents or []
        allowed_actions = allowed_actions or []
        allowed_resources = allowed_resources or []
        self._name = name
        self._agents = agents
        self._allowed_actions = allowed_actions
        self._allowed_resources = allowed_resources
        self._request_space = PolicySpace(
            agents=list(agents),
            actions=list(allowed_actions),
            resources=list(allowed_resources),
        )

    def evaluate(self, request: EvalRequest) -> EvalResult:
        """Evaluate the RBAC policy: agent, action, and resource glob match."""
        start = time.monotonic()

        reason_parts = []
        denied = False

        if request.agent not in self._agents:
            denied = True
            reason_parts.append(f"agent '{request.agent}' not in allowed agents")

        if not denied and request.action not in self._allowed_actions:
            denied = True
            reason_parts.append(f"action '{request.action}' not in allowed actions")

        if not denied:
            resource_match = any(
                fnmatch.fnmatch(request.resource, pattern)
                for pattern in self._allowed_resources
            )
            if not resource_match:
                denied = True
                reason_parts.append(
                    f"resource '{request.resource}' does not match any allowed pattern"
                )

        elapsed = (time.monotonic() - start) * 1000.0
        effect = Effect.DENY if denied else Effect.ALLOW
        reason = "; ".join(reason_parts) if reason_parts else "RBAC check passed"

        return EvalResult(
            effect=effect,
            guardrail_name=self.name,
            reason=reason,
            eval_time_ms=elapsed,
            metadata={
                "rbac_result": {
                    "agent": request.agent,
                    "agent_permitted": request.agent in self._agents,
                    "action_permitted": request.action in self._allowed_actions,
                },
            },
        )

    def explain(self, result: EvalResult) -> Explanation:
        """Produce an RBAC-specific explanation."""
        return Explanation(
            decision_id=result.metadata.get("request_id"),
            effect=result.effect,
            guardrail_name=result.guardrail_name,
            rbac_result=result.metadata.get("rbac_result"),
            eval_time_ms=result.eval_time_ms,
        )
