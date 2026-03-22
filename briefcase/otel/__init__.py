"""Minimal OpenTelemetry helpers for Briefcase integrations."""

try:
    from opentelemetry import trace
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "briefcase.otel requires the 'otel' extra.\n"
        "Install it with: pip install briefcase-ai[otel]"
    ) from exc

__all__ = ["get_tracer"]


def get_tracer(name: str = "briefcase"):
    """Return a tracer configured for the given component name."""
    return trace.get_tracer(name)
