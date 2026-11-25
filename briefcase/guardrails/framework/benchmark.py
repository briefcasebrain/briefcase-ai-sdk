"""Systematic guardrail benchmarking.

Part of :mod:`briefcase.guardrails.framework`, which re-exports every name
defined here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import (
    Dict,
    List,
    Optional,
)

from briefcase.guardrails._types import (
    Effect,
    EvalResult,
)
from briefcase.guardrails.framework.adversarial import (
    GuardrailInjection,
    GuardrailTask,
    InjectionOutcome,
)
from briefcase.guardrails.framework.env import (
    GuardrailEnv,
)


# ============================================================================
# 8. BENCHMARK — Systematic evaluation (AgentDojo-inspired)
# ============================================================================

@dataclass
class BenchmarkResult:
    """Results for a single (task, injection) pair."""
    task_id: str
    injection_id: Optional[str]
    utility: bool       # did guardrail produce expected effect?
    security: bool      # did guardrail resist injection?
    result: EvalResult  # raw evaluation result
    injection_outcome: InjectionOutcome = InjectionOutcome.NOT_APPLICABLE


@dataclass
class BenchmarkReport:
    """Aggregated benchmark output. Like agentdojo.SuiteResults."""
    total_tasks: int
    total_injections: int
    total_evaluations: int
    utility_score: float          # % tasks with correct effect (no injection)
    security_score: float         # % tasks where injection was resisted
    results: List[BenchmarkResult]
    by_category: Dict[str, Dict[str, float]] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    def summary(self) -> str:
        lines = [
            f"Benchmark: {self.total_evaluations} evaluations in {self.elapsed_ms:.0f}ms",
            f"  Utility:  {self.utility_score:.1%} ({self.total_tasks} tasks)",
            f"  Security: {self.security_score:.1%} ({self.total_injections} injections)",
        ]
        if self.by_category:
            lines.append("  By category:")
            for cat, scores in sorted(self.by_category.items()):
                lines.append(
                    f"    {cat}: utility={scores.get('utility', 0):.1%}, "
                    f"security={scores.get('security', 0):.1%}"
                )
        return "\n".join(lines)


class GuardrailBenchmark:
    """Systematic evaluation of a GuardrailEnv against tasks and injections.

    Like agentdojo.benchmark_suite_with_injections, but for guardrails.

    The benchmark matrix:
      - For each task (no injection): evaluate → check utility
      - For each (task, injection) pair: inject → evaluate → check security

    This replaces the hardcoded smt_fuzzer.py with an extensible framework.
    Anyone can write new GuardrailTask and GuardrailInjection implementations
    for their domain.
    """

    def run(
        self,
        env: GuardrailEnv,
        tasks: List[GuardrailTask],
        injections: Optional[List[GuardrailInjection]] = None,
    ) -> BenchmarkReport:
        start = time.monotonic()
        results: List[BenchmarkResult] = []
        injections = injections or []

        # Phase 1: Utility (no injections)
        utility_pass = 0
        for task in tasks:
            result = env.evaluate(task.request)
            passed = task.utility(result)
            if passed:
                utility_pass += 1
            results.append(BenchmarkResult(
                task_id=task.id,
                injection_id=None,
                utility=passed,
                security=True,
                result=result,
            ))

        # Phase 2: Security (with injections)
        security_pass = 0
        security_total = 0
        for task in tasks:
            for injection in injections:
                security_total += 1
                adversarial_request = injection.inject(task.request)
                try:
                    result = env.evaluate(adversarial_request)
                    secure = injection.security(result, task)
                    outcome = InjectionOutcome.RESISTED if secure else InjectionOutcome.BYPASSED
                except Exception as exc:
                    result = EvalResult(
                        effect=Effect.DENY,
                        guardrail_name=env.name,
                        reason=f"Crashed: {type(exc).__name__}: {exc}",
                        metadata={"error": True, "exception": str(exc)},
                    )
                    secure = True  # crash = not bypassed
                    outcome = InjectionOutcome.CRASHED
                if secure:
                    security_pass += 1
                results.append(BenchmarkResult(
                    task_id=task.id,
                    injection_id=injection.id,
                    utility=task.utility(result),
                    security=secure,
                    result=result,
                    injection_outcome=outcome,
                ))

        elapsed = (time.monotonic() - start) * 1000.0

        # Aggregate by category
        by_category: Dict[str, Dict[str, List[bool]]] = {}
        for r in results:
            cat = next(
                (t.category for t in tasks if t.id == r.task_id),
                "default"
            )
            if cat not in by_category:
                by_category[cat] = {"utility": [], "security": []}
            by_category[cat]["utility"].append(r.utility)
            by_category[cat]["security"].append(r.security)

        cat_scores = {}
        for cat, lists in by_category.items():
            cat_scores[cat] = {
                "utility": sum(lists["utility"]) / max(len(lists["utility"]), 1),
                "security": sum(lists["security"]) / max(len(lists["security"]), 1),
            }

        return BenchmarkReport(
            total_tasks=len(tasks),
            total_injections=len(injections),
            total_evaluations=len(results),
            utility_score=utility_pass / max(len(tasks), 1),
            security_score=security_pass / max(security_total, 1),
            results=results,
            by_category=cat_scores,
            elapsed_ms=elapsed,
        )
