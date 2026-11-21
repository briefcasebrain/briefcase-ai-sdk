"""Model drift detection helpers.

``DriftCalculator`` and ``DriftMetrics`` (consistency inside one batch) are
backed by the native extension and resolve on first use, so the pure-Python
baseline-versus-window comparison (:mod:`briefcase.drift.baseline`) imports
without the compiled core.
"""

from typing import Any

_NATIVE_NAMES = ("DriftCalculator", "DriftMetrics")


def __getattr__(name: str) -> Any:
    if name not in _NATIVE_NAMES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        from briefcase import _native
        value = getattr(_native, name)
    except (ImportError, AttributeError) as exc:
        raise ImportError(
            "briefcase.drift could not load the native extension. "
            "Reinstall the package (pip install --force-reinstall briefcase-ai) "
            "or rebuild from source with 'maturin develop'. "
            "(briefcase.drift.baseline is pure Python and works without it.)"
        ) from exc
    globals()[name] = value
    return value


__all__ = list(_NATIVE_NAMES)
