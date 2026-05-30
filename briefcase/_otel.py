"""Centralized optional OpenTelemetry import.

OpenTelemetry is an optional dependency (the ``otel`` extra). Importing it in
one place keeps the identical ~6-line ``try/except ImportError`` boilerplate out
of every instrumented module. Call sites do::

    from briefcase._otel import trace, HAS_OTEL

and guard tracing code with ``if HAS_OTEL:``. When OpenTelemetry is not
installed, every exported symbol is ``None`` and ``HAS_OTEL`` is ``False`` so the
guarded code is simply skipped.
"""

try:
    from opentelemetry import context, trace
    from opentelemetry.trace import Status, StatusCode
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )

    HAS_OTEL = True
except ImportError:  # pragma: no cover - exercised only without the otel extra
    context = None  # type: ignore[assignment]
    trace = None  # type: ignore[assignment]
    Status = None  # type: ignore[assignment,misc]
    StatusCode = None  # type: ignore[assignment,misc]
    TraceContextTextMapPropagator = None  # type: ignore[assignment,misc]
    HAS_OTEL = False

__all__ = [
    "trace",
    "context",
    "Status",
    "StatusCode",
    "TraceContextTextMapPropagator",
    "HAS_OTEL",
]
