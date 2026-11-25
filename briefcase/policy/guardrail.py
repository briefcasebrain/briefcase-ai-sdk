"""The tool policy as a guardrail: ``ToolPolicyEnv`` implements ``GuardrailEnv``.

The hook does not import this module (the guardrail framework costs more to
import than the hook's whole budget). It exists so a tool policy composes with
the rest of ``briefcase.guardrails``:

    from briefcase.guardrails import EvalRequest, GuardrailPipeline
    from briefcase.policy.guardrail import ToolPolicyEnv

    pipeline = GuardrailPipeline([ToolPolicyEnv.from_files(), my_other_guardrail])
    result = pipeline.evaluate(EvalRequest(agent="claude-code", action="shell", resource="rm -rf /"))
    result.is_allowed   # False

How the three policy effects map onto the framework's two:

==========  ===============  ==================================================
policy      ``Effect``       ``EvalResult.metadata``
==========  ===============  ==================================================
allow       ``ALLOW``        ``{"decision": "allow"}``
deny        ``DENY``         ``{"decision": "deny"}``
ask         ``DENY``         ``{"decision": "ask"}`` (a pipeline has nobody to ask)
==========  ===============  ==================================================

``mode = "observe"`` is the framework's ``ViolationMode.AUDIT``: the result is
``ALLOW`` and carries ``violation_mode = "audit"`` and ``original_effect``,
the same keys ``ViolationModeWrapper`` writes.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from briefcase.guardrails._types import Effect, EvalRequest, EvalResult, Explanation, ViolationMode
from briefcase.policy import engine, model

ACTIONS = ("shell", "read", "write", "mcp", "web")


def to_call(request: EvalRequest) -> engine.ToolCall:
    """``EvalRequest(agent=<tool>, action=<kind>, resource=<command | path | server.tool | url>)``.
    ``context`` may carry ``cwd`` and ``project_dir``."""
    kind = request.action if request.action in ACTIONS else "other"
    context = request.context or {}
    common: Dict[str, Any] = {"tool": request.agent, "cwd": context.get("cwd"),
                              "project_dir": context.get("project_dir") or context.get("cwd")}
    if kind == "shell":
        return engine.ToolCall("shell", command=request.resource, **common)
    if kind in ("read", "write"):
        return engine.ToolCall(kind, paths=[request.resource], **common)
    if kind == "mcp":
        server, _, name = request.resource.partition(".")
        return engine.ToolCall("mcp", server=server or None, name=name or None, **common)
    if kind == "web":
        return engine.ToolCall("web", url=request.resource, **common)
    return engine.ToolCall("other", **common)


class ToolPolicyEnv:
    """A :class:`briefcase.guardrails.GuardrailEnv` over a merged tool policy."""

    def __init__(self, policy: model.Policy, name: str = "tool-policy") -> None:
        self._policy = policy
        self._name = name

    @classmethod
    def from_files(cls, project_dir: Optional[str] = None) -> "ToolPolicyEnv":
        loaded = model.load(project_dir)
        if loaded.policy is None:
            detail = "; ".join(text for _, _, text in loaded.errors) or "no policy file found"
            raise model.PolicyError(f"no usable policy: {detail}")
        return cls(loaded.policy)

    @classmethod
    def from_text(cls, text: str) -> "ToolPolicyEnv":
        return cls(model.merge([model.parse_layer(text, "user")]))

    @property
    def name(self) -> str:
        return self._name

    @property
    def request_space(self):
        from briefcase.guardrails.framework.spaces import PolicySpace
        return PolicySpace()

    def evaluate(self, request: EvalRequest) -> EvalResult:
        started = time.monotonic()
        decision = engine.evaluate(self._policy, to_call(request))
        metadata: Dict[str, Any] = {
            "decision": decision.would, "request_id": request.request_id,
            "matches": [match.to_dict() for match in decision.matches], "problems": list(decision.problems),
        }
        if decision.match is not None:
            metadata["policy_applied"] = {"name": decision.match.rule_id, "source": decision.match.source,
                                          "condition": decision.match.unit}
        effect = Effect.ALLOW if decision.would == "allow" else Effect.DENY
        if decision.mode == "observe" and effect == Effect.DENY:
            metadata.update(violation_mode=ViolationMode.AUDIT.value, original_effect="deny")
            effect = Effect.ALLOW
        return EvalResult(effect=effect, guardrail_name=self._name, reason=decision.reason or None,
                          policy_id=decision.rule_id, eval_time_ms=(time.monotonic() - started) * 1000.0,
                          metadata=metadata)

    def explain(self, result: EvalResult) -> Explanation:
        return Explanation(decision_id=result.metadata.get("request_id"), effect=result.effect,
                           guardrail_name=result.guardrail_name,
                           policy_applied=result.metadata.get("policy_applied"), eval_time_ms=result.eval_time_ms)

    def reset(self, config: Optional[Dict[str, Any]] = None) -> None:
        if config and isinstance(config.get("policy_text"), str):
            self._policy = model.merge([model.parse_layer(config["policy_text"], "user")])

    def close(self) -> None:
        pass
