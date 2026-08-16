"""Attribute-based access control guardrail with rule evaluation."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from briefcase.guardrails.framework import (
    BaseGuardrailEnv,
    Effect,
    EvalRequest,
    EvalResult,
    Explanation,
    PolicySpace,
    SpaceBound,
)


@dataclass
class ABACRule:
    """A single attribute-based access control rule.

    ``operator`` is one of ">=", "<=", ">", "<", "==", "!=", "in",
    "not_in". Any other operator never satisfies, so a misconfigured
    rule fails closed with the rule's effect.
    """
    attribute: str
    operator: str
    value: Any
    effect: Effect = Effect.DENY  # returned when the rule is violated


def _check_rule(rule: ABACRule, actual: Any) -> bool:
    """Return True if the rule is satisfied (no violation)."""
    op = rule.operator
    expected = rule.value
    if op == ">=":
        return actual >= expected
    elif op == "<=":
        return actual <= expected
    elif op == ">":
        return actual > expected
    elif op == "<":
        return actual < expected
    elif op == "==":
        return actual == expected
    elif op == "!=":
        return actual != expected
    elif op == "in":
        return actual in expected
    elif op == "not_in":
        return actual not in expected
    return False


class ABACGuardrailEnv(BaseGuardrailEnv):
    """Attribute-based access control guardrail.

    Evaluates a list of rules against the request context. A rule with a
    missing or non-satisfying attribute returns that rule's effect
    immediately. If all rules pass, returns ALLOW.
    """

    def __init__(
        self,
        rules: Optional[List[ABACRule]] = None,
        name: str = "abac",
    ):
        rules = rules or []
        self._name = name
        self._rules = rules
        self._request_space = self._build_space(rules)

    @staticmethod
    def _build_space(rules: List[ABACRule]) -> PolicySpace:
        """Derive a PolicySpace from the rule set."""
        context_schema: Dict[str, SpaceBound] = {}
        for rule in rules:
            if isinstance(rule.value, (int, float)) and rule.operator in (
                ">=", "<=", ">", "<",
            ):
                if rule.operator in (">=", ">"):
                    context_schema[rule.attribute] = SpaceBound(
                        low=float(rule.value), high=float("inf"),
                    )
                else:
                    context_schema[rule.attribute] = SpaceBound(
                        low=float("-inf"), high=float(rule.value),
                    )
            elif rule.attribute not in context_schema:
                context_schema[rule.attribute] = SpaceBound()
        return PolicySpace(context_schema=context_schema)

    def evaluate(self, request: EvalRequest) -> EvalResult:
        """Check all rules against the request context."""
        start = time.monotonic()

        for rule in self._rules:
            actual = request.context.get(rule.attribute)
            if actual is None:
                elapsed = (time.monotonic() - start) * 1000.0
                return EvalResult(
                    effect=rule.effect,
                    guardrail_name=self.name,
                    reason=f"Missing attribute '{rule.attribute}'",
                    eval_time_ms=elapsed,
                    metadata={
                        "violated_rule": {
                            "attribute": rule.attribute,
                            "operator": rule.operator,
                            "expected": rule.value,
                            "actual": None,
                        },
                    },
                )

            if not _check_rule(rule, actual):
                elapsed = (time.monotonic() - start) * 1000.0
                return EvalResult(
                    effect=rule.effect,
                    guardrail_name=self.name,
                    reason=(
                        f"Rule violated: {rule.attribute} {rule.operator} "
                        f"{rule.value} (actual: {actual})"
                    ),
                    eval_time_ms=elapsed,
                    metadata={
                        "violated_rule": {
                            "attribute": rule.attribute,
                            "operator": rule.operator,
                            "expected": rule.value,
                            "actual": actual,
                        },
                    },
                )

        elapsed = (time.monotonic() - start) * 1000.0
        return EvalResult(
            effect=Effect.ALLOW,
            guardrail_name=self.name,
            reason="All ABAC rules passed",
            eval_time_ms=elapsed,
        )

    def explain(self, result: EvalResult) -> Explanation:
        """Produce an ABAC-specific explanation with violated rule detail."""
        violated = result.metadata.get("violated_rule")
        extraction = None
        if violated:
            extraction = {
                "attribute": violated["attribute"],
                "value": violated["actual"],
                "quote": (
                    f"{violated['attribute']} is {violated['actual']}, "
                    f"expected {violated['operator']} {violated['expected']}"
                ),
            }
        return Explanation(
            decision_id=result.metadata.get("request_id"),
            effect=result.effect,
            guardrail_name=result.guardrail_name,
            extraction=extraction,
            abac_result=violated,
            eval_time_ms=result.eval_time_ms,
        )
