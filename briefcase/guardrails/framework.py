"""
Guardrail Framework — Gymnasium/AgentDojo-inspired protocols for Briefcase.

This module defines the universal abstractions that make the guardrail system
a *framework* (extensible, composable, testable) rather than a *library*
(fixed set of classes).

Design inspirations:
  gymnasium.Env      → GuardrailEnv     (universal evaluation protocol)
  gymnasium.Wrapper  → GuardrailWrapper (composable transformations)
  gymnasium.Space    → PolicySpace      (describes the evaluation domain)
  gymnasium.register → guardrail_registry.make() (string-based instantiation)
  gymnasium.VectorEnv → VectorGuardrailEnv (batch evaluation)
  agentdojo.BaseInjectionTask → GuardrailInjection (adversarial protocol)
  agentdojo.benchmark_suite   → GuardrailBenchmark (systematic evaluation)

Why this matters:
  The v2 proposal treats guardrails as configuration objects (dataclasses
  passed to setup()). This works but prevents ecosystem growth — every new
  guardrail type, every new transformation (caching, timeout, sampling),
  and every new test strategy requires changes inside the SDK.

  The framework approach defines protocols. Anyone can implement
  GuardrailEnv for a new domain. Anyone can stack GuardrailWrappers.
  Anyone can write GuardrailInjections for adversarial testing. The SDK
  provides the protocols and a library of built-in implementations.
  The ecosystem provides everything else.

  Gymnasium has >1,000 registered environments because of Env + Space +
  register(). This module aims for the same leverage in the guardrail domain.
"""

from __future__ import annotations

import fnmatch
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    TypeVar,
    runtime_checkable,
)

# Namespace identifier pattern — alphanumeric, hyphens, underscores only
_NAMESPACE_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


# ============================================================================
# 1. CORE DATA TYPES  (extracted to guardrails/_types.py; re-exported here)
# ============================================================================

from briefcase.guardrails._types import (  # noqa: E402
    Effect,
    EvalRequest,
    EvalResult,
    Explanation,
    ViolationMode,
)


# ============================================================================
# 2. SPACE ABSTRACTIONS — Describes what a guardrail evaluates
# ============================================================================
#
# Like gymnasium.spaces.Box/Discrete/Dict, PolicySpace describes the SHAPE
# of valid inputs. This enables:
#   - Automatic input validation before evaluation
#   - Random sampling for testing (fuzz, adversarial, simulation)
#   - Self-documenting guardrails (introspect what they care about)
#   - Type-safe composition (verify two guardrails are compatible)
#

@dataclass
class SpaceBound:
    """Numeric bound for a context attribute."""
    low: float = float("-inf")
    high: float = float("inf")
    dtype: str = "float"  # "float" | "int"


@dataclass
class PolicySpace:
    """Describes the evaluation domain of a GuardrailEnv.

    Analogous to gymnasium.spaces.Dict — a named collection of sub-spaces.
    """
    agents: List[str] = field(default_factory=list)
    actions: List[str] = field(default_factory=lambda: ["access"])
    resources: List[str] = field(default_factory=list)  # glob patterns
    context_schema: Dict[str, SpaceBound] = field(default_factory=dict)

    def contains(self, request: EvalRequest) -> bool:
        """Check if a request is within this space (like Space.contains)."""
        if self.agents and request.agent not in self.agents:
            return False
        if self.actions and request.action not in self.actions:
            return False
        for attr, bound in self.context_schema.items():
            val = request.context.get(attr)
            if val is not None and isinstance(val, (int, float)):
                if val < bound.low or val > bound.high:
                    return False
        return True

    def sample(self, n: int = 1, seed: Optional[int] = None) -> List[EvalRequest]:
        """Generate random valid requests (like Space.sample).

        Useful for fuzz testing, simulation warm-up, and adversarial generation.
        """
        import random
        rng = random.Random(seed)
        results = []
        for _ in range(n):
            ctx = {}
            for attr, bound in self.context_schema.items():
                lo = max(bound.low, -1e6)
                hi = min(bound.high, 1e6)
                if bound.dtype == "int":
                    ctx[attr] = rng.randint(int(lo), int(hi))
                else:
                    ctx[attr] = round(rng.uniform(lo, hi), 4)
            results.append(EvalRequest(
                agent=rng.choice(self.agents) if self.agents else "default",
                action=rng.choice(self.actions) if self.actions else "access",
                resource=rng.choice(self.resources) if self.resources else "/",
                context=ctx,
            ))
        return results

    def boundary_samples(self, n_per_bound: int = 5) -> List[EvalRequest]:
        """Generate samples at boundary conditions (like SMT counter-examples).

        For each numeric context attribute, produces values at low, high, and
        epsilon above/below the bounds. This is the complement to the SMT
        solver — heuristic boundary probing when formal verification is
        unavailable.
        """
        import random
        samples = []
        for attr, bound in self.context_schema.items():
            if bound.low == float("-inf") and bound.high == float("inf"):
                continue
            boundary_values = []
            if bound.low != float("-inf"):
                eps = 1 if bound.dtype == "int" else 0.01
                boundary_values.extend([bound.low - eps, bound.low, bound.low + eps])
            if bound.high != float("inf"):
                eps = 1 if bound.dtype == "int" else 0.01
                boundary_values.extend([bound.high - eps, bound.high, bound.high + eps])
            for val in boundary_values:
                samples.append(EvalRequest(
                    agent=self.agents[0] if self.agents else "default",
                    action=self.actions[0] if self.actions else "access",
                    resource=self.resources[0] if self.resources else "/",
                    context={attr: int(val) if bound.dtype == "int" else val},
                ))
        return samples


# ============================================================================
# 3. GuardrailEnv — The Universal Evaluation Protocol
# ============================================================================
#
# Like gymnasium.Env, this is the minimal interface that EVERY guardrail
# implements. The protocol has 4 methods:
#
#   evaluate(request) → result    (like Env.step)
#   reset(config)     → None      (like Env.reset)
#   explain(result)   → narrative  (no Gym equivalent; from domain requirement)
#   close()           → None      (like Env.close)
#
# Plus 2 space descriptors:
#   request_space     (like Env.observation_space)
#   decision_space    (like Env.action_space — though here it's the output)
#

@runtime_checkable
class GuardrailEnv(Protocol):
    """Universal evaluation protocol for guardrails.

    Any guardrail — RBAC, ABAC, neuro-symbolic, compliance profile, custom —
    implements this interface. The SDK, runtime, wrappers, benchmarks, and
    tooling all program against this protocol, not against concrete classes.

    Gymnasium parallel:
      Env.step(action) → (obs, reward, terminated, truncated, info)
      GuardrailEnv.evaluate(request) → EvalResult

    Why a Protocol (not ABC):
      @runtime_checkable Protocol allows duck typing. A user can implement
      evaluate() without inheriting from any base class. This matches
      Gymnasium's approach where third-party envs often implement the
      interface without subclassing gymnasium.Env directly.
    """

    @property
    def request_space(self) -> PolicySpace:
        """Describes valid evaluation inputs. Like Env.observation_space."""
        ...

    @property
    def name(self) -> str:
        """Human-readable guardrail name."""
        ...

    def evaluate(self, request: EvalRequest) -> EvalResult:
        """Evaluate a request against this guardrail.

        Returns EvalResult with effect=ALLOW or DENY, plus provenance.
        This is the Env.step() equivalent — the core method everything
        depends on.

        Must be:
          - Deterministic (same request → same result, given same config)
          - Side-effect-free (no I/O, no mutation)
          - Fast (<1ms for RBAC, <10ms for ABAC)
        """
        ...

    def explain(self, result: EvalResult) -> Explanation:
        """Produce a human-readable explanation of a result.

        This is domain-specific: a regulated guardrail explains differently
        than a financial one. The Explanation dataclass provides structured +
        narrative + compliance formats.
        """
        ...

    def reset(self, config: Optional[Dict[str, Any]] = None) -> None:
        """Reinitialize with new configuration.

        Like Env.reset() — clears internal state, applies new config.
        Used when hot-reloading guardrails from lakeFS without restarting.
        """
        ...

    def close(self) -> None:
        """Release resources. Like Env.close()."""
        ...


# ============================================================================
# 4. ABSTRACT BASE — Convenience base class (like gymnasium.Env)
# ============================================================================

class BaseGuardrailEnv(ABC):
    """Abstract base class providing defaults for GuardrailEnv protocol.

    Like gymnasium.Env, this provides default implementations for reset(),
    close(), and explain() so that concrete guardrails only need to implement
    evaluate() and define request_space and name.

    Subclassing is optional — the Protocol is the contract, not this class.
    """

    _request_space: PolicySpace = PolicySpace()
    _name: str = "unnamed"

    @property
    def request_space(self) -> PolicySpace:
        return self._request_space

    @property
    def name(self) -> str:
        return self._name

    @abstractmethod
    def evaluate(self, request: EvalRequest) -> EvalResult:
        ...

    def explain(self, result: EvalResult) -> Explanation:
        """Default explanation from EvalResult metadata."""
        return Explanation(
            decision_id=result.metadata.get("request_id"),
            effect=result.effect,
            guardrail_name=result.guardrail_name,
            extraction=result.metadata.get("extraction"),
            policy_applied=result.metadata.get("policy_applied"),
            rbac_result=result.metadata.get("rbac_result"),
            abac_result=result.metadata.get("abac_result"),
            lakefs_sha=result.lakefs_sha,
            eval_time_ms=result.eval_time_ms,
        )

    def reset(self, config: Optional[Dict[str, Any]] = None) -> None:
        pass

    def close(self) -> None:
        pass


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

    Gymnasium parallel: gymnasium.wrappers.TimeLimit caches episode length;
    this caches evaluation results.
    """

    def __init__(self, env: GuardrailEnv, ttl_seconds: float = 60.0):
        super().__init__(env)
        self._ttl = ttl_seconds
        self._store: Dict[str, Tuple[EvalResult, float]] = {}

    def evaluate(self, request: EvalRequest) -> EvalResult:
        key = request.cache_key()
        entry = self._store.get(key)
        if entry is not None:
            result, inserted_at = entry
            if time.monotonic() - inserted_at <= self._ttl:
                cached = EvalResult(
                    effect=result.effect,
                    guardrail_name=result.guardrail_name,
                    reason=result.reason,
                    policy_id=result.policy_id,
                    lakefs_sha=result.lakefs_sha,
                    eval_time_ms=0.0,
                    metadata={**result.metadata, "cache_hit": True},
                )
                return cached
            del self._store[key]

        result = self.env.evaluate(request)
        self._store[key] = (result, time.monotonic())
        return result


class TimeoutWrapper(GuardrailWrapper):
    """Hard timeout with configurable fallback effect.

    If evaluation exceeds max_ms, returns fallback_effect (default: DENY,
    preserving deny-by-default). Like gymnasium.wrappers.TimeLimit but for
    wall-clock time rather than step count.
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
        start = time.monotonic()
        result = self.env.evaluate(request)
        elapsed_ms = (time.monotonic() - start) * 1000.0
        if elapsed_ms > self._max_ms:
            return EvalResult(
                effect=self._fallback,
                guardrail_name=self.name,
                reason=f"Timeout: {elapsed_ms:.1f}ms > {self._max_ms}ms",
                eval_time_ms=elapsed_ms,
                metadata={"timeout": True, "original_effect": result.effect.value},
            )
        result.eval_time_ms = elapsed_ms
        return result


class AuditWrapper(GuardrailWrapper):
    """Records every evaluation for observability.

    Appends (request, result) to an in-memory log that can be flushed
    to OTel, a webhook, or a file. Like gymnasium.wrappers.RecordVideo
    but for evaluation traces.
    """

    def __init__(self, env: GuardrailEnv, max_records: int = 10000):
        super().__init__(env)
        self._log: List[Tuple[EvalRequest, EvalResult]] = []
        self._max = max_records

    def evaluate(self, request: EvalRequest) -> EvalResult:
        result = self.env.evaluate(request)
        if len(self._log) < self._max:
            self._log.append((request, result))
        return result

    @property
    def audit_log(self) -> List[Tuple[EvalRequest, EvalResult]]:
        return list(self._log)

    def flush(self) -> List[Tuple[EvalRequest, EvalResult]]:
        log = self._log
        self._log = []
        return log


class SamplingWrapper(GuardrailWrapper):
    """Evaluate only a fraction of requests; allow the rest.

    Useful for performance-sensitive paths where full evaluation is too
    expensive. Like gymnasium.wrappers.RecordEpisodeStatistics with a
    trigger function, but for guardrails.
    """

    def __init__(self, env: GuardrailEnv, rate: float = 1.0, seed: int = 42):
        super().__init__(env)
        self._rate = rate
        import random
        self._rng = random.Random(seed)

    def evaluate(self, request: EvalRequest) -> EvalResult:
        if self._rng.random() > self._rate:
            return EvalResult(
                effect=Effect.ALLOW,
                guardrail_name=self.name,
                reason="Sampling: skipped",
                eval_time_ms=0.0,
                metadata={"sampled_out": True},
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
    """

    def __init__(self, envs: List[GuardrailEnv]):
        self._envs = envs

    @property
    def num_envs(self) -> int:
        return len(self._envs)

    def evaluate_batch(
        self,
        requests: List[EvalRequest],
    ) -> List[EvalResult]:
        """Evaluate N requests against N envs (1:1 mapping).

        If len(requests) != num_envs, broadcasts:
          - 1 env, N requests: same env for all
          - N envs, 1 request: same request for all
        """
        if len(self._envs) == 1:
            env = self._envs[0]
            return [env.evaluate(r) for r in requests]
        if len(requests) == 1:
            req = requests[0]
            return [env.evaluate(req) for env in self._envs]
        if len(requests) != len(self._envs):
            raise ValueError(
                f"Batch size mismatch: {len(requests)} requests vs "
                f"{len(self._envs)} envs. Use 1:N or N:1 broadcasting."
            )
        return [
            env.evaluate(req) for env, req in zip(self._envs, requests)
        ]

    @staticmethod
    def from_single(env: GuardrailEnv, n: int = 1) -> "VectorGuardrailEnv":
        """Create N copies of the same env for parallel request evaluation."""
        return VectorGuardrailEnv([env] * n)


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


# ============================================================================
# 9. REGISTRY — String-based instantiation (gym.register / gym.make)
# ============================================================================
#
# This is THE critical abstraction for ecosystem growth. Gymnasium has
# >1,000 registered environments because of gym.register() + gym.make().
#
# Without a registry, every new guardrail type requires an import and
# explicit construction. With a registry, it's:
#
#   guardrails.make("pii-check-v1", confidence_threshold=0.85)
#
# Three registries, mirroring Gymnasium's layered approach:
#   - GuardrailRegistry   → envs (gym.envs.registry)
#   - WrapperRegistry     → wrappers (gym.wrappers)
#   - InjectionRegistry   → adversarial tests (agentdojo.injections)
#

@dataclass
class EnvSpec:
    """Specification for a registered guardrail environment.

    Like gymnasium.envs.registration.EnvSpec — stores everything needed
    to reconstruct a GuardrailEnv from a string identifier.

    The entry_point is a string in "module:ClassName" format, resolved
    lazily at make() time. This allows registration without importing
    the implementation (critical for plugin discovery).
    """
    id: str                               # e.g. "rbac-env-v1"
    entry_point: str                      # e.g. "mypackage.guardrails:MyGuardrailEnv"
    kwargs: Dict[str, Any] = field(default_factory=dict)
    version: Optional[int] = None         # extracted from id if "-vN" suffix
    namespace: Optional[str] = None       # e.g. "acme" → "acme/pii-check-v1"
    description: str = ""
    tags: List[str] = field(default_factory=list)  # e.g. ["rbac", "access-control"]
    max_eval_time_ms: Optional[float] = None  # performance contract


class GuardrailRegistry:
    """Global registry for string-based guardrail instantiation.

    Like gymnasium.envs.registry but for guardrails.

    Usage:
        registry = GuardrailRegistry()
        registry.register(
            id="my-check-v1",
            entry_point="mypackage.guardrails:MyGuardrailEnv",
            kwargs={"confidence_threshold": 0.85},
            tags=["access-control"],
        )

        env = registry.make("my-check-v1")
        env = registry.make("my-check-v1", confidence_threshold=0.9)  # override

    The register/make split enables:
      - Plugin authors register guardrails at import time
      - Users instantiate by string without knowing the module path
      - CI/CD systems enumerate all registered guardrails for testing
      - Domain packs register bundles of guardrails in one call
    """

    def __init__(self) -> None:
        self._specs: Dict[str, EnvSpec] = {}

    def register(
        self,
        id: str,
        entry_point: str,
        kwargs: Optional[Dict[str, Any]] = None,
        namespace: Optional[str] = None,
        description: str = "",
        tags: Optional[List[str]] = None,
        max_eval_time_ms: Optional[float] = None,
    ) -> None:
        """Register a guardrail environment.

        Args:
            id: Unique identifier, e.g. "pii-check-v1". Convention:
                {domain}-{name}-v{version}
            entry_point: "module.path:ClassName" — resolved at make() time
            kwargs: Default constructor arguments
            namespace: Optional vendor/org prefix. If provided, the
                effective id becomes "namespace/id" (e.g. "acme/pii-check-v1").
                The env can be made via either the full namespaced id or
                the short id (if unambiguous).
            description: Human-readable description
            tags: Searchable tags for discovery
            max_eval_time_ms: Performance contract (optional)
        """
        if namespace and not _NAMESPACE_RE.match(namespace):
            raise ValueError(
                f"Invalid namespace '{namespace}': must match [a-zA-Z0-9_-]+"
            )

        effective_id = f"{namespace}/{id}" if namespace else id

        if effective_id in self._specs:
            raise ValueError(f"Guardrail '{effective_id}' already registered")

        version = None
        bare_id = id  # version extracted from the bare id, not the namespace
        if "-v" in bare_id:
            try:
                version = int(bare_id.rsplit("-v", 1)[1])
            except (ValueError, IndexError):
                pass

        self._specs[effective_id] = EnvSpec(
            id=effective_id,
            entry_point=entry_point,
            kwargs=kwargs or {},
            version=version,
            namespace=namespace,
            description=description,
            tags=tags or [],
            max_eval_time_ms=max_eval_time_ms,
        )

    def _resolve_id(self, id: str) -> str:
        """Resolve a potentially short id to its full registered key.

        Lookup order:
          1. Exact match (works for both namespaced and bare ids)
          2. Short-id fallback: if id has no '/', search for specs
             whose bare suffix matches. Succeeds only if exactly one
             match exists (unambiguous).
          3. Version-aware fallback: if id has no '-v' suffix and no '/',
             search for specs matching '{id}-v{N}' and resolve to the
             highest version number.
        """
        if id in self._specs:
            return id
        if "/" not in id:
            matches = [
                key for key in self._specs
                if "/" in key and key.split("/", 1)[1] == id
            ]
            if len(matches) == 1:
                return matches[0]

            # Version-aware: if no '-v' suffix, search for versioned variants
            if "-v" not in id:
                versioned = []
                for key, spec in self._specs.items():
                    # Match bare keys like '{id}-v{N}' or namespaced '{ns}/{id}-v{N}'
                    bare = key.split("/", 1)[1] if "/" in key else key
                    if bare.startswith(id + "-v") and spec.version is not None:
                        versioned.append((spec.version, key))
                if versioned:
                    versioned.sort(key=lambda x: x[0], reverse=True)
                    return versioned[0][1]

        return id  # will fail at caller with KeyError if not found

    def make(self, id: str, **kwargs: Any) -> GuardrailEnv:
        """Instantiate a registered guardrail by id.

        Like gym.make() — resolves the entry_point, merges kwargs,
        and returns a ready-to-use GuardrailEnv.

        Accepts both full namespaced ids ("acme/pii-check-v1") and
        short ids ("pii-check-v1") when unambiguous.

        Args:
            id: Registered guardrail identifier
            **kwargs: Override default constructor arguments
        """
        resolved = self._resolve_id(id)
        spec = self._specs.get(resolved)
        if spec is None:
            available = ", ".join(sorted(self._specs.keys())) or "(none)"
            raise KeyError(
                f"Guardrail '{id}' not registered. Available: {available}"
            )

        cls = self._resolve_entry_point(spec.entry_point)
        merged = {**spec.kwargs, **kwargs}
        env = cls(**merged)

        if not isinstance(env, GuardrailEnv):
            raise TypeError(
                f"{spec.entry_point} returned {type(env).__name__}, "
                f"which does not satisfy GuardrailEnv protocol"
            )
        return env

    def spec(self, id: str) -> EnvSpec:
        """Get the spec for a registered guardrail."""
        resolved = self._resolve_id(id)
        if resolved not in self._specs:
            raise KeyError(f"Guardrail '{id}' not registered")
        return self._specs[resolved]

    def list(
        self,
        tags: Optional[List[str]] = None,
        namespace: Optional[str] = None,
    ) -> List[EnvSpec]:
        """List registered guardrails, optionally filtered by tags/namespace.

        Args:
            tags: If provided, only return specs matching ALL tags.
            namespace: If provided, only return specs in this namespace.
        """
        specs = list(self._specs.values())
        if tags:
            tag_set = set(tags)
            specs = [s for s in specs if tag_set.issubset(set(s.tags))]
        if namespace is not None:
            specs = [s for s in specs if s.namespace == namespace]
        return sorted(specs, key=lambda s: s.id)

    def _resolve_entry_point(self, entry_point: str) -> type:
        """Resolve 'module.path:ClassName' to a class object."""
        if ":" not in entry_point:
            raise ValueError(
                f"entry_point must be 'module:ClassName', got: {entry_point}"
            )
        module_path, class_name = entry_point.rsplit(":", 1)
        import importlib
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        return cls


# Module-level singleton — like gymnasium.envs.registry
_default_registry = GuardrailRegistry()

def register(id: str, entry_point: str, **kwargs: Any) -> None:
    """Register a guardrail in the global registry.

    Convenience function — equivalent to guardrail_registry.register().

    Usage:
        from briefcase.guardrails.framework import register, make

        register("my-guardrail-v1", "my_package:MyGuardrail", threshold=0.9)
        env = make("my-guardrail-v1")
    """
    _default_registry.register(id=id, entry_point=entry_point, **kwargs)


def make(id: str, **kwargs: Any) -> GuardrailEnv:
    """Instantiate a guardrail from the global registry.

    Convenience function — equivalent to guardrail_registry.make().
    """
    return _default_registry.make(id, **kwargs)


def list_registered(tags: Optional[List[str]] = None) -> List[EnvSpec]:
    """List all registered guardrails in the global registry."""
    return _default_registry.list(tags=tags)


# ============================================================================
# 10. SPACE ALGEBRA — Composition checking
# ============================================================================
#
# Like gymnasium.spaces.utils (flatdim, flatten, unflatten) but for
# PolicySpace. Enables answering: "Can these two guardrails be composed?"
# "What's the combined evaluation domain of this pipeline?"
#

class SpaceAlgebra:
    """Operations on PolicySpaces for composition checking.

    When stacking guardrails, you need to know if they're compatible.
    SpaceAlgebra provides union (what either accepts), intersection
    (what both accept), and compatibility checking.

    Gymnasium parallel: gymnasium.spaces.utils provides flatten/unflatten;
    we provide set-algebraic operations because guardrail composition is
    inherently a set theory problem (who can do what with which data).
    """

    @staticmethod
    def union(a: PolicySpace, b: PolicySpace) -> PolicySpace:
        """Union of two spaces — accepts inputs valid in EITHER space.

        Useful for AnyOf composition: if either guardrail accepts the
        request shape, the composition does too.
        """
        agents = list(set(a.agents) | set(b.agents)) if a.agents or b.agents else []
        actions = list(set(a.actions) | set(b.actions)) if a.actions or b.actions else []
        resources = list(set(a.resources) | set(b.resources)) if a.resources or b.resources else []

        # For context schema: take the wider bounds
        all_attrs = set(list(a.context_schema.keys()) + list(b.context_schema.keys()))
        context_schema = {}
        for attr in all_attrs:
            bound_a = a.context_schema.get(attr, SpaceBound())
            bound_b = b.context_schema.get(attr, SpaceBound())
            context_schema[attr] = SpaceBound(
                low=min(bound_a.low, bound_b.low),
                high=max(bound_a.high, bound_b.high),
                dtype=bound_a.dtype,
            )

        return PolicySpace(
            agents=sorted(agents),
            actions=sorted(actions),
            resources=sorted(resources),
            context_schema=context_schema,
        )

    @staticmethod
    def intersection(a: PolicySpace, b: PolicySpace) -> PolicySpace:
        """Intersection of two spaces — accepts inputs valid in BOTH spaces.

        Useful for AllOf composition: both guardrails must accept the
        request shape for the composition to evaluate it.
        """
        # If either has agents, intersect; if one is empty (no constraint), use the other
        if a.agents and b.agents:
            agents = sorted(set(a.agents) & set(b.agents))
        elif a.agents:
            agents = list(a.agents)
        elif b.agents:
            agents = list(b.agents)
        else:
            agents = []

        if a.actions and b.actions:
            actions = sorted(set(a.actions) & set(b.actions))
        elif a.actions:
            actions = list(a.actions)
        elif b.actions:
            actions = list(b.actions)
        else:
            actions = []

        if a.resources and b.resources:
            resources = SpaceAlgebra._glob_intersect_resources(
                a.resources, b.resources,
            )
        elif a.resources:
            resources = list(a.resources)
        elif b.resources:
            resources = list(b.resources)
        else:
            resources = []

        # For context schema: take the tighter bounds
        all_attrs = set(list(a.context_schema.keys()) + list(b.context_schema.keys()))
        context_schema = {}
        for attr in all_attrs:
            bound_a = a.context_schema.get(attr, SpaceBound())
            bound_b = b.context_schema.get(attr, SpaceBound())
            context_schema[attr] = SpaceBound(
                low=max(bound_a.low, bound_b.low),
                high=min(bound_a.high, bound_b.high),
                dtype=bound_a.dtype,
            )

        return PolicySpace(
            agents=agents,
            actions=actions,
            resources=resources,
            context_schema=context_schema,
        )

    @staticmethod
    def is_compatible(a: PolicySpace, b: PolicySpace) -> bool:
        """Check if two spaces have a non-empty intersection.

        Returns False if composing them would create an impossible
        evaluation domain (e.g., no shared agents, disjoint resources,
        or inverted bounds).
        """
        inter = SpaceAlgebra.intersection(a, b)

        # Check for empty discrete intersections
        if a.agents and b.agents and not inter.agents:
            return False
        if a.actions and b.actions and not inter.actions:
            return False
        if a.resources and b.resources and not inter.resources:
            return False

        # Check for inverted numeric bounds
        for attr, bound in inter.context_schema.items():
            if bound.low > bound.high:
                return False

        return True

    @staticmethod
    def _glob_intersect_resources(
        a_resources: List[str], b_resources: List[str],
    ) -> List[str]:
        """Compute glob-aware resource intersection.

        Two resources overlap if either fnmatch-matches the other.
        When a pattern matches a literal, the more specific (literal)
        form is kept.  When two patterns match each other (e.g. both
        identical), one copy is kept.
        """
        result: List[str] = []
        for ra in a_resources:
            for rb in b_resources:
                if ra == rb:
                    if ra not in result:
                        result.append(ra)
                elif fnmatch.fnmatch(rb, ra):
                    # ra is a broader pattern that matches rb
                    if rb not in result:
                        result.append(rb)
                elif fnmatch.fnmatch(ra, rb):
                    # rb is a broader pattern that matches ra
                    if ra not in result:
                        result.append(ra)
        return sorted(result)

    @staticmethod
    def dimensionality(space: PolicySpace) -> Dict[str, int]:
        """Report the cardinality of each dimension.

        Like gymnasium.spaces.utils.flatdim — useful for understanding
        the size of the evaluation domain.
        """
        return {
            "agents": len(space.agents) if space.agents else -1,  # -1 = unbounded
            "actions": len(space.actions) if space.actions else -1,
            "resources": len(space.resources) if space.resources else -1,
            "context_attrs": len(space.context_schema),
        }


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
    ):
        self._stages = stages
        self._mode = mode
        self._name = name

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

        for stage in self._stages:
            result = stage.evaluate(request)
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
    The sync evaluate() runs in the current event loop (no thread pool)
    because it should be <1ms.
    """

    def __init__(self, env: GuardrailEnv):
        self._env = env

    @property
    def request_space(self) -> PolicySpace:
        return self._env.request_space

    @property
    def name(self) -> str:
        return self._env.name

    async def evaluate(self, request: EvalRequest) -> EvalResult:
        return self._env.evaluate(request)

    async def explain(self, result: EvalResult) -> Explanation:
        return self._env.explain(result)

    async def reset(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._env.reset(config)

    async def close(self) -> None:
        self._env.close()


# ============================================================================
# 13. RENDERING / OBSERVABILITY PROTOCOL
# ============================================================================
#
# Gymnasium has Env.render() for visualization. For guardrails, "rendering"
# means structured observability output — not pixels but audit data.
#
# This protocol lets any env or wrapper emit structured observations
# that tools can consume (dashboards, CLI reporters, OTel exporters).
#

@runtime_checkable
class Renderable(Protocol):
    """Optional protocol for guardrails that support structured rendering.

    Like gymnasium.Env.render() but produces structured data instead
    of pixel buffers. Consumed by dashboards, CLI reporters, and
    OTel span exporters.

    Usage:
        if isinstance(env, Renderable):
            for frame in env.render():
                dashboard.push(frame)
    """

    def render(self, mode: str = "human") -> Dict[str, Any]:
        """Produce a structured observation.

        Modes:
          "human"     → Dict with readable strings
          "json"      → Dict with JSON-serializable values
          "otel"      → Dict with OTel attribute conventions
        """
        ...


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
        import random
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
