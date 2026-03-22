"""Model drift detection helpers."""

try:
    from briefcase._native import DriftCalculator, DriftMetrics
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "briefcase.drift requires the 'drift' extra.\n"
        "Install it with: pip install briefcase-ai[drift]"
    ) from exc

__all__ = ["DriftCalculator", "DriftMetrics"]
