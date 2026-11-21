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
        "briefcase.replay needs the compiled briefcase core (briefcase._native), "
        "which could not be imported. No pip extra provides it: install a "
        "prebuilt wheel with 'pip install briefcase-ai' (the base package), or "
        "from a source checkout build it with 'maturin develop'. "
        f"Underlying error: {exc}"
    ) from exc

__all__ = [
    "ReplayEngine",
    "ReplayPolicy",
    "ReplayResult",
    "ReplayStats",
]
