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
        "briefcase.sanitize could not load the native extension. "
        "Reinstall the package (pip install --force-reinstall briefcase-ai) "
        "or rebuild from source with 'maturin develop'."
    ) from exc

__all__ = [
    "Redaction",
    "SanitizationJsonResult",
    "SanitizationResult",
    "Sanitizer",
]
