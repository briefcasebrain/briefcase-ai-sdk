"""One-line observability setup: :func:`briefcase.observe`."""

from __future__ import annotations

from typing import Optional, Union

from briefcase.config import setup
from briefcase.exporters.base import BaseExporter
from briefcase.exporters.console import ConsoleExporter
from briefcase.exporters.file import JSONLFileExporter
from briefcase.exporters.memory import MemoryExporter


def _resolve_exporter(exporter: Union[str, BaseExporter, None]) -> BaseExporter:
    if isinstance(exporter, BaseExporter):
        return exporter
    if exporter is None or exporter == "console":
        return ConsoleExporter()
    if exporter == "memory":
        return MemoryExporter()
    if isinstance(exporter, str) and exporter.endswith(".jsonl"):
        return JSONLFileExporter(exporter)
    raise ValueError(
        f"Unknown exporter shorthand: {exporter!r}. Pass a BaseExporter instance, "
        "'console', 'memory', or a path ending in '.jsonl'."
    )


def observe(
    exporter: Union[str, BaseExporter, None] = "console",
    *,
    level: Optional[Union[int, str]] = None,
) -> BaseExporter:
    """Wire up decision-capture export in one call.

    Without this, ``@capture`` records decisions but has nowhere to send them.
    ``observe`` configures the global exporter so captured records are emitted.

    Args:
        exporter: A :class:`~briefcase.exporters.base.BaseExporter` instance, or a
            shorthand string:
              - ``"console"`` (default) → :class:`ConsoleExporter`
              - ``"memory"`` → :class:`MemoryExporter`
              - a path ending in ``.jsonl`` → :class:`JSONLFileExporter`
        level: If given, also enable briefcase logging at this level
            (see :func:`briefcase.enable_logging`).

    Returns:
        The configured exporter — so a :class:`MemoryExporter` can be inspected.

    Example:
        import briefcase
        mem = briefcase.observe("memory")

        @briefcase.capture(async_capture=False)
        def classify(text): return text.upper()

        classify("hello")
        print(mem.records)  # -> [{'decision_id': ..., 'function_name': 'classify', ...}]
    """
    resolved = _resolve_exporter(exporter)
    setup(exporter=resolved)
    if level is not None:
        from briefcase._logging import enable_logging

        enable_logging(level)
    return resolved


__all__ = ["observe"]
