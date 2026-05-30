"""Storage backends backed by the native Briefcase runtime."""

try:
    from briefcase._native import BufferedBackend, SqliteBackend
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "briefcase.storage could not load the native extension. "
        "Reinstall the package (pip install --force-reinstall briefcase-ai) "
        "or rebuild from source with 'maturin develop'."
    ) from exc

__all__ = ["BufferedBackend", "SqliteBackend"]
