"""Replay utilities backed by the native Briefcase engine."""

try:
    from briefcase._native import (
        ReplayEngine,
        ReplayPolicy,
        ReplayResult,
        ReplayStats,
    )
except ImportError as exc:  # pragma: no cover - handled at import time
    raise ImportError(
        "briefcase.replay requires the 'replay' extra.\n"
        "Install it with: pip install briefcase-ai[replay]"
    ) from exc

__all__ = [
    "ReplayEngine",
    "ReplayPolicy",
    "ReplayResult",
    "ReplayStats",
]
