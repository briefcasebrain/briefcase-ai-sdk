"""The GuardrailEnv protocol and its convenience base class.

Part of :mod:`briefcase.guardrails.framework`, which re-exports every name
defined here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
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
from briefcase.guardrails.framework.spaces import (
    PolicySpace,
)


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
