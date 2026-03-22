"""Guardrail framework — Gymnasium/AgentDojo-inspired protocols for Briefcase.

Re-exports all public API symbols for convenient access:
    from briefcase.guardrails import Effect, EvalRequest, GuardrailEnv, make
"""

try:
    # --- Core data types ---
    from briefcase.guardrails.framework import (
        Effect,
        ViolationMode,
        EvalRequest,
        EvalResult,
        Explanation,
        SpaceBound,
        PolicySpace,
    )

    # --- Protocols and base classes ---
    from briefcase.guardrails.framework import (
        GuardrailEnv,
        AsyncGuardrailEnv,
        BaseGuardrailEnv,
        Renderable,
    )

    # --- Wrappers ---
    from briefcase.guardrails.framework import (
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

    # --- Pipeline ---
    from briefcase.guardrails.framework import (
        PipelineMode,
        PipelineResult,
        GuardrailPipeline,
        SyncAdapter,
    )

    # --- Batch ---
    from briefcase.guardrails.framework import VectorGuardrailEnv

    # --- Adversarial ---
    from briefcase.guardrails.framework import (
        GuardrailTask,
        GuardrailInjection,
        ContextSwapInjection,
        BoundaryProbeInjection,
        UnicodeNormalizationInjection,
        NullByteInjection,
        OversizedInputInjection,
        InjectionOutcome,
        BenchmarkResult,
        BenchmarkReport,
        GuardrailBenchmark,
    )

    # --- Curriculum ---
    from briefcase.guardrails.framework import CurriculumConfig, GuardrailCurriculum

    # --- Registry ---
    from briefcase.guardrails.framework import (
        EnvSpec,
        GuardrailRegistry,
        register,
        make,
        list_registered,
    )

    # --- Space algebra ---
    from briefcase.guardrails.framework import SpaceAlgebra
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "briefcase.guardrails requires the 'guardrails' extra.\n"
        "Install it with: pip install briefcase-ai[guardrails]"
    ) from exc

__all__ = [
    # Core types
    "Effect",
    "ViolationMode",
    "EvalRequest",
    "EvalResult",
    "Explanation",
    "SpaceBound",
    "PolicySpace",
    # Protocols
    "GuardrailEnv",
    "AsyncGuardrailEnv",
    "BaseGuardrailEnv",
    "Renderable",
    # Wrappers
    "GuardrailWrapper",
    "RequestTransformWrapper",
    "ResultTransformWrapper",
    "CacheWrapper",
    "TimeoutWrapper",
    "AuditWrapper",
    "SamplingWrapper",
    "DenyByDefaultWrapper",
    "ViolationModeWrapper",
    # Pipeline
    "PipelineMode",
    "PipelineResult",
    "GuardrailPipeline",
    "SyncAdapter",
    # Batch
    "VectorGuardrailEnv",
    # Adversarial
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
    # Curriculum
    "CurriculumConfig",
    "GuardrailCurriculum",
    # Registry
    "EnvSpec",
    "GuardrailRegistry",
    "register",
    "make",
    "list_registered",
    # Space algebra
    "SpaceAlgebra",
]
