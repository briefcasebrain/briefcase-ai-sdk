"""
Prompt-knowledge validation system.
"""

try:
    from briefcase.validation.errors import (
        ValidationError,
        ValidationErrorCode,
        ValidationReport,
    )
    from briefcase.validation.engine import PromptValidationEngine
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "briefcase.validation requires the 'validate' extra.\n"
        "Install it with: pip install briefcase-ai[validate]"
    ) from exc

from briefcase.validation.engine import Extractor, Resolver, SemanticValidatorProtocol

__all__ = [
    "ValidationError",
    "ValidationErrorCode",
    "ValidationReport",
    "PromptValidationEngine",
    "Extractor",
    "Resolver",
    "SemanticValidatorProtocol",
]
