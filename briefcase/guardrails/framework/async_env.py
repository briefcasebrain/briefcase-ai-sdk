"""Async guardrail protocol and the sync-to-async adapter.

Part of :mod:`briefcase.guardrails.framework`, which re-exports every name
defined here.
"""

from __future__ import annotations

import asyncio
from typing import (
    Any,
    Dict,
    Optional,
    Protocol,
    runtime_checkable,
)

from briefcase.guardrails._types import (
    EvalRequest,
    EvalResult,
    Explanation,
)
from briefcase.guardrails.framework.env import (
    GuardrailEnv,
)
from briefcase.guardrails.framework.spaces import (
    PolicySpace,
)


# ============================================================================
# 12. ASYNC PROTOCOL — For I/O-bound guardrails
# ============================================================================
#
# The sync GuardrailEnv is correct for pure-computation guardrails (RBAC,
# ABAC, Cedar). But some guardrails need I/O:
#   - Remote policy store (lakeFS, OPA)
#   - External attribute lookup (LDAP)
#   - ML model inference (confidence scoring)
#
# AsyncGuardrailEnv mirrors the sync protocol but with async/await.
# This matches the codebase pattern: routing/base.py uses async def route().
#

@runtime_checkable
class AsyncGuardrailEnv(Protocol):
    """Async variant of GuardrailEnv for I/O-bound guardrails.

    Use when evaluate() needs network I/O (remote policy store, external
    attribute lookup, ML model inference).

    Mirrors the sync protocol exactly — all wrappers and benchmarks that
    work with GuardrailEnv have async equivalents.

    Codebase precedent: BaseRouter.route() is async for the same reason.
    """

    @property
    def request_space(self) -> PolicySpace:
        ...

    @property
    def name(self) -> str:
        ...

    async def evaluate(self, request: EvalRequest) -> EvalResult:
        ...

    async def explain(self, result: EvalResult) -> Explanation:
        ...

    async def reset(self, config: Optional[Dict[str, Any]] = None) -> None:
        ...

    async def close(self) -> None:
        ...


class SyncAdapter:
    """Wraps a sync GuardrailEnv to satisfy AsyncGuardrailEnv.

    Allows sync envs to be used in async pipelines without code changes.
    ``evaluate()`` runs the sync env on the event loop's default thread
    pool, so a guardrail that blocks (I/O, a slow model, a lock) does not
    stall every other task on the loop.

    Pass ``run_in_thread=False`` to call the env inline instead. That saves
    the thread hop (tens of microseconds) and is right only for guardrails
    that are pure computation and return in well under a millisecond, or
    that are not safe to call off the loop thread.
    """

    def __init__(self, env: GuardrailEnv, run_in_thread: bool = True):
        self._env = env
        self._run_in_thread = run_in_thread

    @property
    def request_space(self) -> PolicySpace:
        return self._env.request_space

    @property
    def name(self) -> str:
        return self._env.name

    async def evaluate(self, request: EvalRequest) -> EvalResult:
        if not self._run_in_thread:
            return self._env.evaluate(request)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._env.evaluate, request)

    async def explain(self, result: EvalResult) -> Explanation:
        return self._env.explain(result)

    async def reset(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._env.reset(config)

    async def close(self) -> None:
        self._env.close()
