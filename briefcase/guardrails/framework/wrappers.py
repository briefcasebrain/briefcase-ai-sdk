"""Composable GuardrailWrapper transformations and the built-in wrappers.

Part of :mod:`briefcase.guardrails.framework`, which re-exports every name
defined here.
"""

from __future__ import annotations

import random
import threading
import time
from abc import abstractmethod
from collections import OrderedDict, deque
from dataclasses import replace
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
)

from briefcase._logging import get_logger
from briefcase.guardrails._types import (
    Effect,
    EvalRequest,
    EvalResult,
    Explanation,
    ViolationMode,
)
from briefcase.guardrails.framework.env import (
    GuardrailEnv,
)
from briefcase.guardrails.framework.spaces import (
    PolicySpace,
)

logger = get_logger(__name__)


# ============================================================================
# 5. WRAPPER PATTERN — Composable transformations
# ============================================================================
#
# Like gymnasium.Wrapper, a GuardrailWrapper IS a GuardrailEnv. It wraps
# another env, delegating by default and overriding selectively.
#
# This is the key architectural insight from Gymnasium: wrappers compose
# infinitely. CacheWrapper(TimeoutWrapper(AuditWrapper(env))) works
# because each wrapper is itself a GuardrailEnv.
#
# Gymnasium has specialized wrappers (ActionWrapper, ObservationWrapper,
# RewardWrapper). We have analogous specializations:
#   RequestWrapper  → transforms the request before evaluation
#   ResultWrapper   → transforms the result after evaluation
#   FilterWrapper   → short-circuits evaluation under certain conditions
#

class GuardrailWrapper:
    """Base wrapper — delegates everything to the wrapped env.

    Like gymnasium.Wrapper: subclass and override only what you transform.

    Usage:
        class MyWrapper(GuardrailWrapper):
            def evaluate(self, request):
                # pre-processing
                result = self.env.evaluate(request)
                # post-processing
                return result

        wrapped = MyWrapper(CacheWrapper(base_env))
    """

    def __init__(self, env: GuardrailEnv):
        self.env = env

    @property
    def request_space(self) -> PolicySpace:
        return self.env.request_space

    @property
    def name(self) -> str:
        return self.env.name

    def evaluate(self, request: EvalRequest) -> EvalResult:
        return self.env.evaluate(request)

    def explain(self, result: EvalResult) -> Explanation:
        return self.env.explain(result)

    def reset(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.env.reset(config)

    def close(self) -> None:
        self.env.close()

    @property
    def unwrapped(self) -> GuardrailEnv:
        """Access the base env through all wrapper layers.
        Like gymnasium.Wrapper.unwrapped."""
        env = self.env
        while isinstance(env, GuardrailWrapper):
            env = env.env
        return env


# --- Specialized wrapper types ---

class RequestTransformWrapper(GuardrailWrapper):
    """Override transform_request() to modify the request before evaluation.
    Like gymnasium.ActionWrapper."""

    def evaluate(self, request: EvalRequest) -> EvalResult:
        return self.env.evaluate(self.transform_request(request))

    @abstractmethod
    def transform_request(self, request: EvalRequest) -> EvalRequest:
        ...


class ResultTransformWrapper(GuardrailWrapper):
    """Override transform_result() to modify the result after evaluation.
    Like gymnasium.ObservationWrapper."""

    def evaluate(self, request: EvalRequest) -> EvalResult:
        result = self.env.evaluate(request)
        return self.transform_result(result)

    @abstractmethod
    def transform_result(self, result: EvalResult) -> EvalResult:
        ...


# --- Built-in wrappers ---

class CacheWrapper(GuardrailWrapper):
    """LRU cache with TTL, exposed as a composable
    wrapper usable on ANY GuardrailEnv.

    Holds at most max_entries results; inserting beyond the bound evicts
    the least recently used entry.

    Gymnasium parallel: gymnasium.wrappers.TimeLimit caches episode length;
    this caches evaluation results.
    """

    def __init__(
        self,
        env: GuardrailEnv,
        ttl_seconds: float = 60.0,
        max_entries: int = 1024,
    ):
        super().__init__(env)
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._store: "OrderedDict[str, Tuple[EvalResult, float]]" = OrderedDict()
        # Guards _store: lookups reorder the LRU and inserts evict, so
        # unsynchronized concurrent calls can corrupt it.
        self._lock = threading.Lock()

    def evaluate(self, request: EvalRequest) -> EvalResult:
        key = request.cache_key()
        with self._lock:
            entry = self._store.get(key)
            if entry is not None:
                result, inserted_at = entry
                if time.monotonic() - inserted_at <= self._ttl:
                    self._store.move_to_end(key)
                    return replace(
                        result,
                        eval_time_ms=0.0,
                        metadata={**result.metadata, "cache_hit": True},
                    )
                del self._store[key]

        # The inner evaluation runs outside the lock so a slow guardrail
        # does not serialize every caller; concurrent misses for one key
        # each evaluate and the last write wins.
        result = self.env.evaluate(request)
        # Keep a private copy: callers and outer wrappers may mutate the
        # result they are handed without changing what the cache serves.
        stored = replace(result, metadata=dict(result.metadata))
        with self._lock:
            self._store[key] = (stored, time.monotonic())
            self._store.move_to_end(key)
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)
        return result


class TimeoutWrapper(GuardrailWrapper):
    """Deadline with a configurable fallback effect.

    Evaluation runs on a worker thread and the caller waits at most max_ms for
    it. Past the deadline the wrapper returns fallback_effect (default: DENY,
    preserving deny-by-default) without waiting any longer.

    Python cannot cancel a running call, so the wrapped guardrail keeps
    executing on its daemon thread and its result is discarded. The deadline
    bounds the caller's wait, not the guardrail's work: a guardrail that blocks
    forever leaks one thread per evaluation.

    The worker thread costs about 45us per evaluation. Stack CacheWrapper
    inside this one when that matters.
    """

    def __init__(
        self,
        env: GuardrailEnv,
        max_ms: float = 50.0,
        fallback_effect: Effect = Effect.DENY,
    ):
        super().__init__(env)
        self._max_ms = max_ms
        self._fallback = fallback_effect

    def evaluate(self, request: EvalRequest) -> EvalResult:
        outcome: List[Any] = []

        def _run() -> None:
            try:
                outcome.append((True, self.env.evaluate(request)))
            except BaseException as exc:  # surfaced to the caller below
                outcome.append((False, exc))

        start = time.monotonic()
        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(timeout=self._max_ms / 1000.0)
        elapsed_ms = (time.monotonic() - start) * 1000.0

        if worker.is_alive() or not outcome:
            return EvalResult(
                effect=self._fallback,
                guardrail_name=self.name,
                reason=f"Timeout: exceeded {self._max_ms}ms deadline",
                eval_time_ms=elapsed_ms,
                metadata={"timeout": True},
            )

        succeeded, value = outcome[0]
        if not succeeded:
            raise value
        # Return a copy: the inner env (a CacheWrapper, for one) may still
        # hold this object, and stamping the wait time onto it in place would
        # rewrite the cached result.
        return replace(value, eval_time_ms=elapsed_ms)


class AuditWrapper(GuardrailWrapper):
    """Records every evaluation for observability.

    Keeps (request, result) pairs in an in-memory ring buffer that can be
    flushed to OTel, a webhook, or a file. Like gymnasium.wrappers.RecordVideo
    but for evaluation traces.

    The buffer holds the most recent ``max_records`` evaluations. When it is
    full the oldest record is overwritten, ``dropped`` counts how many were
    lost, and a warning is logged once per fill (again after each
    ``flush()``), so recording never stops silently. Flush more often, or
    raise ``max_records``, to keep everything.
    """

    def __init__(self, env: GuardrailEnv, max_records: int = 10000):
        super().__init__(env)
        if max_records < 1:
            raise ValueError("max_records must be >= 1")
        self._max = max_records
        self._log: "deque[Tuple[EvalRequest, EvalResult]]" = deque(maxlen=max_records)
        self._lock = threading.Lock()
        self._dropped = 0
        self._warned = False

    def evaluate(self, request: EvalRequest) -> EvalResult:
        result = self.env.evaluate(request)
        warn = False
        with self._lock:
            if len(self._log) == self._max:
                self._dropped += 1
                if not self._warned:
                    self._warned = warn = True
            self._log.append((request, result))
        if warn:
            logger.warning(
                "AuditWrapper(%s): audit log reached max_records=%d; the oldest "
                "records are now being overwritten. Call flush() more often or "
                "raise max_records.",
                self.name, self._max,
            )
        return result

    @property
    def audit_log(self) -> List[Tuple[EvalRequest, EvalResult]]:
        with self._lock:
            return list(self._log)

    @property
    def dropped(self) -> int:
        """Records overwritten since construction because the buffer was full."""
        return self._dropped

    def flush(self) -> List[Tuple[EvalRequest, EvalResult]]:
        with self._lock:
            log = list(self._log)
            self._log.clear()
            self._warned = False
        return log


class SamplingWrapper(GuardrailWrapper):
    """Evaluate only a fraction of requests with the inner guardrail.

    Useful for performance-sensitive paths where full evaluation is too
    expensive. Like gymnasium.wrappers.RecordEpisodeStatistics with a
    trigger function, but for guardrails.

    A request that is not sampled is **not evaluated**, so its outcome is a
    policy choice, set by ``skip_effect``:

    - ``Effect.DENY`` (default): fail closed. Unsampled requests are denied.
    - ``Effect.ALLOW``: fail open. Unsampled requests pass unchecked; choose
      this only where the guardrail is advisory (shadow evaluation, audit
      sampling) and something else enforces access.

    Either way the result says so: ``metadata["sampled_out"]`` and
    ``metadata["skipped"]`` are ``True``, ``metadata["evaluated"]`` is
    ``False`` and ``metadata["skip_effect"]`` names the effect applied.
    Sampled requests carry ``metadata["evaluated"] = True`` only if the
    inner guardrail sets it; the absence of ``sampled_out`` marks a real
    evaluation.
    """

    def __init__(
        self,
        env: GuardrailEnv,
        rate: float = 1.0,
        seed: int = 42,
        skip_effect: Effect = Effect.DENY,
    ):
        super().__init__(env)
        if not 0.0 <= rate <= 1.0:
            raise ValueError(f"rate must be between 0.0 and 1.0, got {rate!r}")
        self._rate = rate
        self._skip_effect = Effect(skip_effect)
        self._rng = random.Random(seed)

    @property
    def skip_effect(self) -> Effect:
        return self._skip_effect

    def evaluate(self, request: EvalRequest) -> EvalResult:
        # random() is in [0.0, 1.0): rate=1.0 always evaluates, rate=0.0 never.
        if self._rng.random() >= self._rate:
            return EvalResult(
                effect=self._skip_effect,
                guardrail_name=self.name,
                reason=(
                    "Sampling: not evaluated (sampled out); "
                    f"skip_effect={self._skip_effect.value}"
                ),
                eval_time_ms=0.0,
                metadata={
                    "sampled_out": True,
                    "skipped": True,
                    "evaluated": False,
                    "skip_effect": self._skip_effect.value,
                },
            )
        return self.env.evaluate(request)


class DenyByDefaultWrapper(GuardrailWrapper):
    """Catches any exception from the inner env and returns DENY.

    This is the Cedar deny-by-default invariant as a composable wrapper.
    Apply it as the outermost wrapper to guarantee that evaluation errors
    never result in accidental access.
    """

    def evaluate(self, request: EvalRequest) -> EvalResult:
        try:
            return self.env.evaluate(request)
        except Exception as exc:
            return EvalResult(
                effect=Effect.DENY,
                guardrail_name=self.name,
                reason=f"Deny-by-default: {type(exc).__name__}: {exc}",
                eval_time_ms=0.0,
                metadata={"error": True, "exception": str(exc)},
            )


class ViolationModeWrapper(GuardrailWrapper):
    """Applies ViolationMode semantics to evaluation results.

    Transforms DENY results based on the configured violation mode:
      - BLOCK: pass DENY through unchanged (zero overhead, default)
      - WARN: convert DENY → ALLOW, annotate metadata
      - AUDIT: same as WARN but tagged as audit

    Use this to implement soft-deny workflows where violations are
    logged rather than enforced.
    """

    def __init__(self, env: GuardrailEnv, mode: ViolationMode = ViolationMode.BLOCK):
        super().__init__(env)
        self._mode = mode

    @property
    def violation_mode(self) -> ViolationMode:
        return self._mode

    def evaluate(self, request: EvalRequest) -> EvalResult:
        result = self.env.evaluate(request)
        if result.effect == Effect.DENY and self._mode != ViolationMode.BLOCK:
            return EvalResult(
                effect=Effect.ALLOW,
                guardrail_name=result.guardrail_name,
                reason=result.reason,
                policy_id=result.policy_id,
                lakefs_sha=result.lakefs_sha,
                eval_time_ms=result.eval_time_ms,
                metadata={
                    **result.metadata,
                    "violation_mode": self._mode.value,
                    "original_effect": "deny",
                },
            )
        return result
