"""Model drift detection helpers."""

try:
    from briefcase._native import DriftCalculator, DriftMetrics
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "briefcase.drift could not load the native extension. "
        "Reinstall the package (pip install --force-reinstall briefcase-ai) "
        "or rebuild from source with 'maturin develop'."
    ) from exc

__all__ = ["DriftCalculator", "DriftMetrics"]
