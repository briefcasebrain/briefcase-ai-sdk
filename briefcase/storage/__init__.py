"""Storage backends backed by the native Briefcase runtime."""

try:
    from briefcase._native import BufferedBackend, SqliteBackend
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "briefcase.storage requires the 'storage' extra.\n"
        "Install it with: pip install briefcase-ai[storage]"
    ) from exc

__all__ = ["BufferedBackend", "SqliteBackend"]
