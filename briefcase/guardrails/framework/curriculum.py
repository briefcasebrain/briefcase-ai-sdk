"""Curriculum: test-case generation from benchmark results.

Part of :mod:`briefcase.guardrails.framework`, which re-exports every name
defined here.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import (
    List,
    Optional,
)

from briefcase.guardrails._types import (
    EvalRequest,
)
from briefcase.guardrails.framework.adversarial import (
    GuardrailInjection,
    GuardrailTask,
)
from briefcase.guardrails.framework.benchmark import (
    BenchmarkReport,
    GuardrailBenchmark,
)
from briefcase.guardrails.framework.env import (
    GuardrailEnv,
)


# ============================================================================
# 14. CURRICULUM — Automatic test case generation from benchmark results
# ============================================================================
#
# AgentDojo runs tasks once. A curriculum runs them iteratively, focusing
# on failure modes. This is the "training loop" for guardrails.
#
# After a benchmark run, the curriculum generates harder test cases
# targeting the categories with lowest scores. This replaces manual
# red-teaming with automated adversarial curriculum learning.
#

@dataclass
class CurriculumConfig:
    """Configuration for curriculum-based testing."""
    focus_threshold: float = 0.9    # categories below this get extra tests
    amplification_factor: int = 3   # how many extra tests per weak category
    max_rounds: int = 5             # maximum curriculum iterations
    seed: int = 42


class GuardrailCurriculum:
    """Iterative benchmark refinement — generates harder tests from results.

    Like an RL curriculum: easy cases first, then focus on failure modes.

    Usage:
        curriculum = GuardrailCurriculum()
        final_report = curriculum.run(env, initial_tasks, injections)
        # final_report.by_category shows where the guardrail is weakest
    """

    def __init__(self, config: Optional[CurriculumConfig] = None):
        self._config = config or CurriculumConfig()

    def identify_weak_categories(
        self, report: BenchmarkReport
    ) -> List[str]:
        """Find categories scoring below the focus threshold."""
        weak = []
        for cat, scores in report.by_category.items():
            if scores.get("utility", 1.0) < self._config.focus_threshold:
                weak.append(cat)
            if scores.get("security", 1.0) < self._config.focus_threshold:
                weak.append(cat)
        return list(set(weak))

    def amplify_tasks(
        self,
        tasks: List[GuardrailTask],
        weak_categories: List[str],
        env: GuardrailEnv,
    ) -> List[GuardrailTask]:
        """Generate additional tasks focused on weak categories.

        Uses PolicySpace.boundary_samples() to create adversarial
        variants in the weak categories.
        """
        rng = random.Random(self._config.seed)
        amplified: List[GuardrailTask] = []

        weak_tasks = [t for t in tasks if t.category in weak_categories]
        if not weak_tasks:
            return []

        for task in weak_tasks:
            for i in range(self._config.amplification_factor):
                # Perturb context values slightly
                new_ctx = dict(task.request.context)
                for key, val in new_ctx.items():
                    if isinstance(val, (int, float)):
                        noise = rng.gauss(0, abs(val) * 0.1 + 0.01)
                        new_ctx[key] = type(val)(val + noise)

                amplified.append(GuardrailTask(
                    id=f"{task.id}-amp-{i}",
                    request=EvalRequest(
                        agent=task.request.agent,
                        action=task.request.action,
                        resource=task.request.resource,
                        context=new_ctx,
                    ),
                    expected_effect=task.expected_effect,
                    category=task.category,
                ))

        return amplified

    def run(
        self,
        env: GuardrailEnv,
        tasks: List[GuardrailTask],
        injections: Optional[List[GuardrailInjection]] = None,
    ) -> BenchmarkReport:
        """Run iterative curriculum — benchmarks + amplification loop."""
        benchmark = GuardrailBenchmark()
        all_tasks = list(tasks)
        latest_report = benchmark.run(env, all_tasks, injections)

        for _round in range(self._config.max_rounds):
            weak = self.identify_weak_categories(latest_report)
            if not weak:
                break  # all categories above threshold

            new_tasks = self.amplify_tasks(all_tasks, weak, env)
            if not new_tasks:
                break

            all_tasks.extend(new_tasks)
            latest_report = benchmark.run(env, all_tasks, injections)

        return latest_report
