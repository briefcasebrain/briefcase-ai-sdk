"""Cost analysis utilities."""

try:
    from briefcase._native import BudgetAlert, BudgetStatus, CostCalculator, CostEstimate
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "briefcase.cost requires the 'drift' extra.\n"
        "Install it with: pip install briefcase-ai[drift]"
    ) from exc

__all__ = [
    "BudgetAlert",
    "BudgetStatus",
    "CostCalculator",
    "CostEstimate",
]
