"""
Main validation engine coordinating extraction, resolution, and semantic layers.

The engine is a pure framework — it orchestrates validation but does not include
any built-in extractors, resolvers, or semantic validators. Provide your own
implementations or install briefcase-ai-enterprise for pre-built ones.

Usage:
    engine = PromptValidationEngine(
        extractor=my_extractor,   # must have .extract(prompt) -> list
        resolver=my_resolver,     # must have .resolve_all(refs) -> list[ValidationError]
        lakefs_client=client,
        repository="my-repo",
    )
    report = engine.validate("Check document ref:abc123")
"""

import time
from typing import Any, Protocol, runtime_checkable

from briefcase._logging import get_logger
from briefcase._otel import trace, HAS_OTEL
from briefcase.validation.errors import ValidationReport
from briefcase.semantic_conventions.validation import *  # noqa: F403

logger = get_logger(__name__)
tracer = trace.get_tracer(__name__) if HAS_OTEL else None


@runtime_checkable
class Extractor(Protocol):
    """Protocol for reference extractors."""
    def extract(self, prompt: str) -> list: ...


@runtime_checkable
class Resolver(Protocol):
    """Protocol for reference resolvers."""
    def resolve_all(self, references: list) -> list: ...


@runtime_checkable
class SemanticValidatorProtocol(Protocol):
    """Protocol for semantic validators."""
    def validate_semantic(self, prompt: str, references: list) -> list: ...


class PromptValidationEngine:
    """
    Multi-layer validation engine for prompt-knowledge consistency.

    Accepts pluggable extractor, resolver, and semantic validator implementations.
    """

    def __init__(
        self,
        extractor: Any,
        resolver: Any,
        lakefs_client: Any,
        repository: str,
        branch: str = "main",
        mode: str = "strict",
        semantic_validator: Any = None,
    ):
        if mode not in ("strict", "tolerant", "warn_only"):
            raise ValueError(f"unknown validation mode: {mode!r}")
        self.extractor = extractor
        self.resolver = resolver
        self.semantic = semantic_validator
        self.mode = mode
        self.repository = repository
        self.branch = branch
        self.lakefs = lakefs_client

    def _commit_sha(self) -> str:
        """The knowledge-base commit the report is pinned to, or "unknown" when
        the lakeFS client cannot supply one.

        Accepts both client contracts: VersionedClient.get_commit() takes no
        arguments, while older adapters take (repository, branch). Calling the
        wrong one loses provenance silently, so the mismatch is retried rather
        than swallowed."""
        try:
            return self.lakefs.get_commit()
        except TypeError:
            try:
                return self.lakefs.get_commit(self.repository, self.branch)
            except Exception as e:
                logger.debug(
                    "lakeFS commit lookup failed; using 'unknown': %s", e, exc_info=True
                )
                return "unknown"
        except Exception as e:
            logger.debug("lakeFS commit lookup failed; using 'unknown': %s", e, exc_info=True)
            return "unknown"

    def validate(self, prompt: str) -> ValidationReport:
        """
        Validate prompt against knowledge base.
        Returns ValidationReport with all errors and warnings.
        """
        if HAS_OTEL and tracer:
            with tracer.start_as_current_span("validation.validate_prompt") as span:
                return self._validate_with_telemetry(prompt, span)
        else:
            return self._validate_internal(prompt)

    def _validate_with_telemetry(self, prompt: str, span) -> ValidationReport:
        """Validate with telemetry."""
        span.set_attribute(VALIDATION_MODE, self.mode)

        report = self._validate_internal(prompt)

        span.set_attribute(VALIDATION_STATUS, report.status)
        span.set_attribute(VALIDATION_ERROR_COUNT, len(report.errors))
        span.set_attribute(VALIDATION_RESOLUTION_TIME_MS, report.validation_time_ms)

        for error in report.errors:
            span.add_event(
                "validation.error",
                attributes={
                    VALIDATION_ERROR_CODE: error.code.value,
                    VALIDATION_ERROR_MESSAGE: error.message,
                    VALIDATION_ERROR_REFERENCE: error.reference
                }
            )

        return report

    def _validate_internal(self, prompt: str) -> ValidationReport:
        """Internal validation logic."""
        start_time = time.time()
        all_errors = []
        all_warnings = []

        # Layer 1: Extract references
        references = self.extractor.extract(prompt)

        if len(references) == 0:
            commit_sha = self._commit_sha()

            return ValidationReport(
                status="passed",
                errors=[],
                warnings=[],
                references_checked=0,
                validation_time_ms=(time.time() - start_time) * 1000,
                lakefs_commit=commit_sha
            )

        # Layer 2: Resolve references
        resolution_errors = self.resolver.resolve_all(references)

        for error in resolution_errors:
            if error.severity == "error":
                all_errors.append(error)
            else:
                all_warnings.append(error)

        # Layer 3: Semantic validation (optional)
        if self.semantic and len(all_errors) == 0:
            semantic_errors = self.semantic.validate_semantic(prompt, references)
            all_warnings.extend(semantic_errors)

        # Determine overall status
        elapsed_ms = (time.time() - start_time) * 1000
        status = self._determine_status(all_errors, all_warnings)

        commit_sha = self._commit_sha()

        return ValidationReport(
            status=status,
            errors=all_errors,
            warnings=all_warnings,
            references_checked=len(references),
            validation_time_ms=elapsed_ms,
            lakefs_commit=commit_sha
        )

    def _determine_status(self, errors: list, warnings: list) -> str:
        """Determine overall validation status based on mode."""
        if self.mode == "strict":
            if len(errors) > 0:
                return "failed"
            elif len(warnings) > 0:
                return "warning"
            else:
                return "passed"
        elif self.mode == "tolerant":
            if len(errors) > 0:
                return "failed"
            else:
                return "passed"
        else:  # warn_only
            return "passed"
