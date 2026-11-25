"""Ordered evaluation chain across several guardrails.

Part of :mod:`briefcase.guardrails.framework`, which re-exports every name
defined here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import (
    List,
)

from briefcase._logging import get_logger
from briefcase.guardrails._types import (
    Effect,
    EvalRequest,
    EvalResult,
)
from briefcase.guardrails.framework.env import (
    GuardrailEnv,
)
from briefcase.guardrails.framework.spaces import (
    SpaceAlgebra,
)

logger = get_logger(__name__)


# ============================================================================
# 11. PIPELINE — Ordered evaluation chain (ASGI middleware pattern)
# ============================================================================
#
# While GuardrailWrapper composes transformations around a SINGLE env,
# Pipeline chains MULTIPLE envs in sequence. This is the guardrail
# equivalent of ASGI middleware or Unix pipes.
#
# Pipeline answers: "Evaluate this request against guardrails A, B, C
# in order. Stop on first deny (or collect all results)."
#

class PipelineMode(str, Enum):
    """How the pipeline handles multiple results."""
    FIRST_DENY = "first_deny"     # short-circuit on first DENY (default)
    ALL = "all"                   # evaluate all, return list
    MAJORITY = "majority"         # majority vote (for ensemble evaluation)


@dataclass
class PipelineResult:
    """Result of a pipeline evaluation."""
    final_effect: Effect
    individual_results: List[EvalResult]
    short_circuited: bool = False
    eval_time_ms: float = 0.0

    @property
    def is_allowed(self) -> bool:
        return self.final_effect == Effect.ALLOW


class GuardrailPipeline:
    """Ordered evaluation chain — evaluates request against N envs.

    Like ASGI middleware or Unix pipes: request flows through each
    guardrail in order. Configurable short-circuit and aggregation.

    Usage:
        pipeline = GuardrailPipeline([rbac_env, abac_env, phi_env])
        result = pipeline.evaluate(request)

        # With wrappers on individual stages:
        pipeline = GuardrailPipeline([
            CacheWrapper(rbac_env),
            TimeoutWrapper(abac_env, max_ms=10.0),
            DenyByDefaultWrapper(phi_env),
        ])

    A stage that raises is treated as a DENY by default (fail closed), with
    the error recorded on that stage's result; pass ``on_error="raise"`` to
    propagate instead.

    Why this exists:
        GuardrailWrapper transforms a single env. Pipeline sequences
        multiple envs. These compose: you can wrap a Pipeline, or put
        wrapped envs into a Pipeline.
    """

    def __init__(
        self,
        stages: List[GuardrailEnv],
        mode: PipelineMode = PipelineMode.FIRST_DENY,
        name: str = "pipeline",
        on_error: str = "deny",
    ):
        """
        Args:
            stages: Guardrails to evaluate, in order.
            mode: How stage results combine (see :class:`PipelineMode`).
            name: Pipeline name.
            on_error: What a stage that raises contributes. ``"deny"``
                (default) fails closed: the stage counts as a DENY whose
                result records the error (``metadata["error"]`` is True).
                ``"raise"`` propagates the exception to the caller.
        """
        if on_error not in ("deny", "raise"):
            raise ValueError(f"on_error must be 'deny' or 'raise', got {on_error!r}")
        self._stages = stages
        self._mode = mode
        self._name = name
        self._on_error = on_error

    @property
    def name(self) -> str:
        return self._name

    @property
    def stages(self) -> List[GuardrailEnv]:
        return list(self._stages)

    def evaluate(self, request: EvalRequest) -> PipelineResult:
        """Evaluate request through all pipeline stages."""
        start = time.monotonic()
        results: List[EvalResult] = []

        for index, stage in enumerate(self._stages):
            try:
                result = stage.evaluate(request)
            except Exception as exc:
                if self._on_error == "raise":
                    raise
                # Fail closed: a guardrail that could not decide denies.
                stage_name = str(getattr(stage, "name", type(stage).__name__))
                logger.warning(
                    "Pipeline %s: stage %d (%s) raised %s; denying",
                    self._name, index, stage_name, type(exc).__name__,
                )
                result = EvalResult(
                    effect=Effect.DENY,
                    guardrail_name=stage_name,
                    reason=f"Stage error (fail closed): {type(exc).__name__}: {exc}",
                    eval_time_ms=(time.monotonic() - start) * 1000.0,
                    metadata={
                        "error": True,
                        "exception": str(exc),
                        "exception_type": type(exc).__name__,
                        "stage_index": index,
                    },
                )
            results.append(result)

            if self._mode == PipelineMode.FIRST_DENY and result.effect == Effect.DENY:
                elapsed = (time.monotonic() - start) * 1000.0
                return PipelineResult(
                    final_effect=Effect.DENY,
                    individual_results=results,
                    short_circuited=True,
                    eval_time_ms=elapsed,
                )

        elapsed = (time.monotonic() - start) * 1000.0

        if self._mode == PipelineMode.MAJORITY:
            allow_count = sum(1 for r in results if r.effect == Effect.ALLOW)
            final = Effect.ALLOW if allow_count > len(results) / 2 else Effect.DENY
        else:
            # ALL or FIRST_DENY (no deny found): final is ALLOW if all allow
            has_deny = any(r.effect == Effect.DENY for r in results)
            final = Effect.DENY if has_deny else Effect.ALLOW

        return PipelineResult(
            final_effect=final,
            individual_results=results,
            short_circuited=False,
            eval_time_ms=elapsed,
        )

    def check_compatibility(self) -> bool:
        """Verify all pipeline stages have compatible request spaces."""
        if len(self._stages) < 2:
            return True
        for i in range(len(self._stages) - 1):
            if not SpaceAlgebra.is_compatible(
                self._stages[i].request_space,
                self._stages[i + 1].request_space,
            ):
                return False
        return True
