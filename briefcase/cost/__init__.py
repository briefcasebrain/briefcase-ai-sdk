"""Cost analysis utilities."""

try:
    from briefcase._native import BudgetStatus, CostCalculator, CostEstimate
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "briefcase.cost could not load the native extension. "
        "Reinstall the package (pip install --force-reinstall briefcase-ai) "
        "or rebuild from source with 'maturin develop'."
    ) from exc

__all__ = [
    "BudgetStatus",
    "CostCalculator",
    "CostEstimate",
]
