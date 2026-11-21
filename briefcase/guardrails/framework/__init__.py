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

Layout:
  The framework is a package. Each concern lives in its own module and this
  ``__init__`` re-exports every name, so ``from briefcase.guardrails.framework
  import X`` keeps working for everything that was importable before the split.

    _types (in briefcase.guardrails)  Effect, EvalRequest, EvalResult, ...
    spaces       PolicySpace, SpaceBound, SpaceAlgebra
    env          GuardrailEnv protocol, BaseGuardrailEnv
    wrappers     GuardrailWrapper and the built-in wrappers
    vector       VectorGuardrailEnv
    adversarial  GuardrailTask, GuardrailInjection and built-in injections
    benchmark    GuardrailBenchmark and its reports
    registry     GuardrailRegistry, register(), make(), list_registered()
    pipeline     GuardrailPipeline
    async_env    AsyncGuardrailEnv, SyncAdapter
    rendering    Renderable
    curriculum   GuardrailCurriculum
"""

from __future__ import annotations

from briefcase.guardrails._types import (
    Effect,
    EvalRequest,
    EvalResult,
    Explanation,
    ViolationMode,
)
from briefcase.guardrails.framework.spaces import (
    SpaceBound,
    PolicySpace,
    SpaceAlgebra,
)
from briefcase.guardrails.framework.env import (
    GuardrailEnv,
    BaseGuardrailEnv,
)
from briefcase.guardrails.framework.wrappers import (
    GuardrailWrapper,
    RequestTransformWrapper,
    ResultTransformWrapper,
    CacheWrapper,
    TimeoutWrapper,
    AuditWrapper,
    SamplingWrapper,
    DenyByDefaultWrapper,
    ViolationModeWrapper,
)
from briefcase.guardrails.framework.vector import (
    VectorGuardrailEnv,
)
from briefcase.guardrails.framework.adversarial import (
    GuardrailTask,
    GuardrailInjection,
    ContextSwapInjection,
    BoundaryProbeInjection,
    UnicodeNormalizationInjection,
    NullByteInjection,
    OversizedInputInjection,
    InjectionOutcome,
)
from briefcase.guardrails.framework.benchmark import (
    BenchmarkResult,
    BenchmarkReport,
    GuardrailBenchmark,
)
from briefcase.guardrails.framework.registry import (  # noqa: F401
    _NAMESPACE_RE,
    EnvSpec,
    GuardrailRegistry,
    _default_registry,
    register,
    make,
    list_registered,
)
from briefcase.guardrails.framework.pipeline import (
    PipelineMode,
    PipelineResult,
    GuardrailPipeline,
)
from briefcase.guardrails.framework.async_env import (
    AsyncGuardrailEnv,
    SyncAdapter,
)
from briefcase.guardrails.framework.rendering import (
    Renderable,
)
from briefcase.guardrails.framework.curriculum import (
    CurriculumConfig,
    GuardrailCurriculum,
)

__all__ = [
    "Effect",
    "EvalRequest",
    "EvalResult",
    "Explanation",
    "ViolationMode",
    "SpaceBound",
    "PolicySpace",
    "SpaceAlgebra",
    "GuardrailEnv",
    "BaseGuardrailEnv",
    "GuardrailWrapper",
    "RequestTransformWrapper",
    "ResultTransformWrapper",
    "CacheWrapper",
    "TimeoutWrapper",
    "AuditWrapper",
    "SamplingWrapper",
    "DenyByDefaultWrapper",
    "ViolationModeWrapper",
    "VectorGuardrailEnv",
    "GuardrailTask",
    "GuardrailInjection",
    "ContextSwapInjection",
    "BoundaryProbeInjection",
    "UnicodeNormalizationInjection",
    "NullByteInjection",
    "OversizedInputInjection",
    "InjectionOutcome",
    "BenchmarkResult",
    "BenchmarkReport",
    "GuardrailBenchmark",
    "EnvSpec",
    "GuardrailRegistry",
    "register",
    "make",
    "list_registered",
    "PipelineMode",
    "PipelineResult",
    "GuardrailPipeline",
    "AsyncGuardrailEnv",
    "SyncAdapter",
    "Renderable",
    "CurriculumConfig",
    "GuardrailCurriculum",
]
