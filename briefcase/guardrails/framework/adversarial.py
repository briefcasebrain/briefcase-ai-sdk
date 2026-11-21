"""Adversarial testing protocol: tasks and injections.

Part of :mod:`briefcase.guardrails.framework`, which re-exports every name
defined here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import (
    Any,
)

from briefcase.guardrails._types import (
    Effect,
    EvalRequest,
    EvalResult,
)


# ============================================================================
# 7. ADVERSARIAL TESTING PROTOCOL — AgentDojo-inspired
# ============================================================================
#
# AgentDojo separates Task (what the agent should do) from Injection (what
# the attacker wants). We adapt this:
#   GuardrailTask    = what the guardrail should decide (expected outcome)
#   GuardrailInjection = adversarial input that tests the boundary
#
# Together with GuardrailBenchmark, this replaces the hardcoded smt_fuzzer.py
# with an extensible protocol.
#

@dataclass
class GuardrailTask:
    """A test case with expected outcome.

    Like agentdojo.BaseUserTask — defines what the guardrail should do.
    """
    id: str
    request: EvalRequest
    expected_effect: Effect
    description: str = ""
    category: str = "default"  # "clean", "negation_trap", "noise_trap", etc.

    def utility(self, result: EvalResult) -> bool:
        """Did the guardrail produce the expected effect?
        Like agentdojo.BaseUserTask.utility."""
        return result.effect == self.expected_effect


@dataclass
class GuardrailInjection:
    """An adversarial modification to an EvalRequest.

    Like agentdojo.BaseInjectionTask — defines what the attacker tries.

    The inject() method transforms a clean request into an adversarial one.
    The security() method checks whether the guardrail resisted.
    """
    id: str
    goal: str  # what the injection is trying to achieve
    category: str = "default"

    def inject(self, request: EvalRequest) -> EvalRequest:
        """Transform a clean request into an adversarial one.

        Override in subclasses for domain-specific injections.
        Default: return unchanged (baseline).
        """
        return request

    def security(self, result: EvalResult, task: GuardrailTask) -> bool:
        """Did the guardrail resist the injection?

        Default: the injection fails if the effect matches the expected
        (i.e., the guardrail was not fooled).
        """
        return result.effect == task.expected_effect


class ContextSwapInjection(GuardrailInjection):
    """Swap a context attribute value — tests extraction robustness.

    Example: swap patient_bmi with a family member's BMI.
    """

    def __init__(
        self,
        id: str,
        attribute: str,
        injected_value: Any,
        goal: str = "",
    ):
        super().__init__(id=id, goal=goal or f"Swap {attribute} to {injected_value}",
                         category="context_swap")
        self._attr = attribute
        self._value = injected_value

    def inject(self, request: EvalRequest) -> EvalRequest:
        new_ctx = dict(request.context)
        new_ctx[self._attr] = self._value
        return EvalRequest(
            agent=request.agent,
            action=request.action,
            resource=request.resource,
            context=new_ctx,
            request_id=request.request_id,
        )


class BoundaryProbeInjection(GuardrailInjection):
    """Probe boundary conditions by shifting a value to the threshold edge.

    Uses the PolicySpace to find the boundary and generates requests at
    threshold ± epsilon.
    """

    def __init__(
        self,
        id: str,
        attribute: str,
        boundary_value: float,
        epsilon: float = 1.0,
    ):
        super().__init__(
            id=id,
            goal=f"Probe {attribute} boundary at {boundary_value}",
            category="boundary_probe",
        )
        self._attr = attribute
        self._boundary = boundary_value
        self._epsilon = epsilon

    def inject(self, request: EvalRequest) -> EvalRequest:
        new_ctx = dict(request.context)
        new_ctx[self._attr] = self._boundary
        return EvalRequest(
            agent=request.agent,
            action=request.action,
            resource=request.resource,
            context=new_ctx,
            request_id=request.request_id,
        )


class UnicodeNormalizationInjection(GuardrailInjection):
    """Replace characters with Unicode homoglyphs to bypass string matching.

    Example: Cyrillic 'а' (U+0430) instead of Latin 'a' (U+0061).
    Tests whether the guardrail normalizes Unicode before comparison.
    """

    # Common Latin→Cyrillic homoglyph substitutions
    _HOMOGLYPHS = {
        "a": "\u0430",  # Cyrillic а
        "e": "\u0435",  # Cyrillic е
        "o": "\u043e",  # Cyrillic о
        "p": "\u0440",  # Cyrillic р
        "c": "\u0441",  # Cyrillic с
        "x": "\u0445",  # Cyrillic х
    }

    def __init__(self, id: str, target: str = "agent", goal: str = ""):
        super().__init__(
            id=id,
            goal=goal or f"Unicode homoglyph substitution on {target}",
            category="unicode_normalization",
        )
        self._target = target

    def _homoglyph(self, text: str) -> str:
        return "".join(self._HOMOGLYPHS.get(ch, ch) for ch in text)

    def inject(self, request: EvalRequest) -> EvalRequest:
        agent = self._homoglyph(request.agent) if self._target == "agent" else request.agent
        resource = self._homoglyph(request.resource) if self._target == "resource" else request.resource
        return EvalRequest(
            agent=agent,
            action=request.action,
            resource=resource,
            context=request.context,
            request_id=request.request_id,
        )


class NullByteInjection(GuardrailInjection):
    """Insert null bytes into resource paths to test path parsing.

    Null bytes can truncate strings in C-based systems. Tests whether
    the guardrail properly handles embedded null characters.
    """

    def __init__(self, id: str, position: str = "middle", goal: str = ""):
        super().__init__(
            id=id,
            goal=goal or f"Null byte injection at {position} of resource",
            category="null_byte",
        )
        self._position = position  # "start", "middle", "end"

    def inject(self, request: EvalRequest) -> EvalRequest:
        resource = request.resource
        if self._position == "start":
            resource = "\x00" + resource
        elif self._position == "end":
            resource = resource + "\x00"
        else:  # middle
            mid = len(resource) // 2
            resource = resource[:mid] + "\x00" + resource[mid:]
        return EvalRequest(
            agent=request.agent,
            action=request.action,
            resource=resource,
            context=request.context,
            request_id=request.request_id,
        )


class OversizedInputInjection(GuardrailInjection):
    """Generate oversized input values to test resource limits.

    Creates 100K+ character strings for agent, resource, or context
    values. Tests guardrail behavior under extreme input sizes.
    """

    def __init__(self, id: str, target: str = "resource", size: int = 100_000, goal: str = ""):
        super().__init__(
            id=id,
            goal=goal or f"Oversized {target} ({size} chars)",
            category="oversized_input",
        )
        self._target = target
        self._size = size

    def inject(self, request: EvalRequest) -> EvalRequest:
        filler = "A" * self._size
        agent = filler if self._target == "agent" else request.agent
        resource = filler if self._target == "resource" else request.resource
        context = request.context
        if self._target == "context":
            context = {**request.context, "oversized_value": filler}
        return EvalRequest(
            agent=agent,
            action=request.action,
            resource=resource,
            context=context,
            request_id=request.request_id,
        )


class InjectionOutcome(str, Enum):
    """Fine-grained outcome of an adversarial injection test."""
    RESISTED = "resisted"           # guardrail correctly handled the injection
    BYPASSED = "bypassed"           # injection fooled the guardrail
    CRASHED = "crashed"             # guardrail raised an exception
    NOT_APPLICABLE = "not_applicable"  # no injection was applied
