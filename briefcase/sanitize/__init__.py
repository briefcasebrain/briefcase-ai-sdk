"""PII sanitization helpers."""

try:
    from briefcase._native import (
        Redaction,
        SanitizationJsonResult,
        SanitizationResult,
        Sanitizer,
    )
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "briefcase.sanitize requires the 'sanitize' extra.\n"
        "Install it with: pip install briefcase-ai[sanitize]"
    ) from exc

__all__ = [
    "Redaction",
    "SanitizationJsonResult",
    "SanitizationResult",
    "Sanitizer",
]
