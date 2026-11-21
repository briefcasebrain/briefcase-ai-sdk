"""Batch evaluation across one or more guardrails.

Part of :mod:`briefcase.guardrails.framework`, which re-exports every name
defined here.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import (
    List,
    Optional,
)

from briefcase.guardrails._types import (
    EvalRequest,
    EvalResult,
)
from briefcase.guardrails.framework.env import (
    GuardrailEnv,
)


# ============================================================================
# 6. VECTOR ENV — Batch evaluation
# ============================================================================
#
# Like gymnasium.vector.VectorEnv, but for guardrails. Enables parallel
# evaluation of N requests in one call — critical for PolicySimulator.replay()
# which may evaluate 100K+ historical decisions.
#

class VectorGuardrailEnv:
    """Batch-evaluate requests across one or more GuardrailEnvs.

    Two modes:
      - Single env, many requests (like VectorEnv with same env)
      - Many envs, one request each (for evaluating same request against
        multiple guardrail configurations — policy comparison)

    Evaluations run in parallel on a thread pool of up to ``max_workers``
    threads; results come back in request order. Threads speed up
    guardrails that wait on I/O (remote policy stores, attribute lookups,
    model calls). Pure-Python guardrails share the interpreter lock and see
    little gain, so pass ``max_workers=1`` to evaluate them inline.

    The envs must be safe to call from several threads at once. The first
    exception raised by any evaluation propagates to the caller.
    """

    def __init__(self, envs: List[GuardrailEnv], max_workers: Optional[int] = None):
        if max_workers is not None and max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self._envs = envs
        self._max_workers = max_workers or min(32, (os.cpu_count() or 1) + 4)

    @property
    def num_envs(self) -> int:
        return len(self._envs)

    @property
    def max_workers(self) -> int:
        return self._max_workers

    def evaluate_batch(
        self,
        requests: List[EvalRequest],
    ) -> List[EvalResult]:
        """Evaluate N requests against N envs (1:1 mapping), in parallel.

        If len(requests) != num_envs, broadcasts:
          - 1 env, N requests: same env for all
          - N envs, 1 request: same request for all

        Results are returned in the order of the pairs, regardless of which
        evaluation finishes first.
        """
        if len(self._envs) == 1:
            pairs = [(self._envs[0], r) for r in requests]
        elif len(requests) == 1:
            pairs = [(env, requests[0]) for env in self._envs]
        elif len(requests) != len(self._envs):
            raise ValueError(
                f"Batch size mismatch: {len(requests)} requests vs "
                f"{len(self._envs)} envs. Use 1:N or N:1 broadcasting."
            )
        else:
            pairs = list(zip(self._envs, requests))

        workers = min(self._max_workers, len(pairs))
        if workers <= 1:
            return [env.evaluate(req) for env, req in pairs]
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="briefcase-guardrail"
        ) as pool:
            # map() yields in submission order.
            return list(pool.map(lambda pair: pair[0].evaluate(pair[1]), pairs))

    @staticmethod
    def from_single(env: GuardrailEnv, n: int = 1) -> "VectorGuardrailEnv":
        """Create N references to the same env for parallel request evaluation."""
        return VectorGuardrailEnv([env] * n)
