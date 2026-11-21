"""Replay utilities.

``ReplayEngine``, ``ReplayPolicy``, ``ReplayResult`` and ``ReplayStats`` are
backed by the native Briefcase engine and resolve on first use, so the
pure-Python replay of recorded provider calls
(:mod:`briefcase.replay.recorded`) imports without the compiled core.
"""

from typing import Any

_NATIVE_NAMES = ("ReplayEngine", "ReplayPolicy", "ReplayResult", "ReplayStats")


def __getattr__(name: str) -> Any:
    if name not in _NATIVE_NAMES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        from briefcase import _native
        value = getattr(_native, name)
    except (ImportError, AttributeError) as exc:
        raise ImportError(
            "briefcase.replay needs the compiled briefcase core (briefcase._native), "
            "which could not be imported. No pip extra provides it: install a "
            "prebuilt wheel with 'pip install briefcase-ai' (the base package), or "
            "from a source checkout build it with 'maturin develop'. "
            "(briefcase.replay.recorded, the replay of recorded provider calls, "
            "is pure Python and works without it.) "
            f"Underlying error: {exc}"
        ) from exc
    globals()[name] = value
    return value


__all__ = list(_NATIVE_NAMES)
