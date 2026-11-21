"""OpenTelemetry exporter for decision records.

Translates decision records into OpenTelemetry spans and ships them to an
OTLP collector. Each exported record becomes one ``briefcase.decision``
span carrying the ``briefcase.*`` attributes. Records may be dicts (what
``@capture`` emits) or objects. The span's start and end come from the
record's ``started_at`` / ``ended_at``, and its parent is the span that was
active when the decision was captured (``@capture`` stores that span
context in the record, so it survives the background export thread).

Install
-------
Requires ``opentelemetry-api`` and ``opentelemetry-sdk``, plus the OTLP
exporter package for the chosen protocol:
``pip install briefcase-ai[otel]``. The imports are lazy, so this module
loads without OpenTelemetry installed and fails with a clear error on
construction.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

from briefcase._logging import get_logger
from briefcase.exporters.base import BaseExporter, get_field

logger = get_logger(__name__)


_OTEL_INSTALL_HINT = (
    "opentelemetry-api and opentelemetry-sdk are required. Install with "
    "'pip install briefcase-ai[otel]'."
)


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_datetime(value: Any) -> Optional[datetime]:
    """Accept a datetime or an ISO 8601 string; naive values are UTC."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _to_ns(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (value - epoch) // timedelta(microseconds=1) * 1000


class OTelExporter(BaseExporter):
    """Export decision records as OpenTelemetry spans.

    Args:
        endpoint: OTLP collector endpoint (e.g. "http://localhost:4317").
        service_name: Value for the ``service.name`` resource attribute.
        protocol: "grpc" or "http". Selects which OTLP exporter package
            is imported.
        span_exporter: Inject a custom SpanExporter (e.g. an in-memory
            exporter for tests). When provided, ``endpoint`` and
            ``protocol`` are not used to build an exporter, but the
            protocol is still recorded.
    """

    def __init__(
        self,
        endpoint: str = "http://localhost:4317",
        service_name: str = "briefcase-ai",
        protocol: str = "grpc",
        span_exporter: Optional[Any] = None,
    ) -> None:
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource, SERVICE_NAME
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.trace import SpanKind
        except ImportError as exc:
            raise ImportError(_OTEL_INSTALL_HINT) from exc

        self._endpoint = endpoint
        self._service_name = service_name
        self._protocol = protocol
        self._trace = trace
        self._span_kind_internal = SpanKind.INTERNAL

        resource = Resource.create({SERVICE_NAME: service_name})

        if span_exporter is not None:
            self._span_exporter = span_exporter
        else:
            self._span_exporter = self._build_exporter(endpoint, protocol)

        self._provider = TracerProvider(resource=resource)
        self._provider.add_span_processor(BatchSpanProcessor(self._span_exporter))
        self._tracer = self._provider.get_tracer("briefcase")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_exporter(endpoint: str, protocol: str) -> Any:
        """Instantiate the OTLP span exporter for the requested protocol."""
        if protocol == "grpc":
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter as GrpcExporter,
                )
            except ImportError as exc:
                raise ImportError(
                    "opentelemetry-exporter-otlp-proto-grpc is required for "
                    "protocol='grpc'. Install it with: "
                    "pip install 'briefcase-ai[otel]' or "
                    "pip install opentelemetry-exporter-otlp-proto-grpc"
                ) from exc
            return GrpcExporter(endpoint=endpoint)

        elif protocol == "http":
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter as HttpExporter,
                )
            except ImportError as exc:
                raise ImportError(
                    "opentelemetry-exporter-otlp-proto-http is required for "
                    "protocol='http'. Install it with: "
                    "pip install 'briefcase-ai[otel]' or "
                    "pip install opentelemetry-exporter-otlp-proto-http"
                ) from exc
            return HttpExporter(endpoint=endpoint)

        else:
            raise ValueError(f"Unsupported protocol: {protocol!r}. Use 'grpc' or 'http'.")

    @staticmethod
    def _extract_attrs(decision: Any) -> dict:
        """Pull the eight required briefcase.* attributes from a decision
        record, which may be a dict (``@capture``) or an object."""
        attrs: dict = {}

        # briefcase.decision.id
        decision_id = (
            get_field(decision, "decision_id")
            or get_field(decision, "id")
            or ""
        )
        attrs["briefcase.decision.id"] = str(decision_id)

        # briefcase.context.version
        context_version = ""
        ctx = get_field(decision, "context")
        if ctx is not None:
            context_version = str(get_field(ctx, "version", "") or "")
        if not context_version:
            context_version = str(get_field(decision, "context_version", "") or "")
        attrs["briefcase.context.version"] = context_version

        # briefcase.confidence
        confidence: float = 0.0
        outputs = get_field(decision, "outputs")
        if outputs:
            if isinstance(outputs, list):
                for out in outputs:
                    c = get_field(out, "confidence")
                    if c is not None:
                        confidence = _as_float(c)
                        break
            elif isinstance(outputs, dict):
                confidence = _as_float(outputs.get("confidence", 0.0))
        conf_direct = get_field(decision, "confidence")
        if conf_direct is not None:
            confidence = _as_float(conf_direct)
        attrs["briefcase.confidence"] = confidence

        # briefcase.routing.action
        routing = get_field(decision, "routing_decision")
        if routing is not None:
            attrs["briefcase.routing.action"] = str(get_field(routing, "action", "unrouted"))
        else:
            attrs["briefcase.routing.action"] = "unrouted"

        # briefcase.model.name / briefcase.model.provider
        model_name = ""
        model_provider = ""
        model_params = get_field(decision, "model_parameters")
        if model_params is not None:
            model_name = str(get_field(model_params, "model_name", "") or "")
            model_provider = str(get_field(model_params, "provider", "") or "")
        attrs["briefcase.model.name"] = model_name
        attrs["briefcase.model.provider"] = model_provider

        # briefcase.execution.time_ms
        attrs["briefcase.execution.time_ms"] = _as_float(
            get_field(decision, "execution_time_ms")
        )

        # briefcase.snapshot.type, inferred from the class name when absent
        snapshot_type = get_field(decision, "snapshot_type")
        if snapshot_type is None:
            cls_name = type(decision).__name__.lower()
            if "batch" in cls_name:
                snapshot_type = "batch"
            elif "session" in cls_name:
                snapshot_type = "session"
            else:
                snapshot_type = "decision"
        attrs["briefcase.snapshot.type"] = str(snapshot_type)

        return attrs

    @staticmethod
    def _span_times(decision: Any) -> Tuple[Optional[int], Optional[int]]:
        """Span start/end in epoch nanoseconds from the record's own
        timestamps, so the span covers the decision rather than the instant
        of export. Either may be None (the SDK then uses "now")."""
        start = _to_datetime(
            get_field(decision, "started_at") or get_field(decision, "timestamp")
        )
        end = _to_datetime(get_field(decision, "ended_at"))
        exec_ms = get_field(decision, "execution_time_ms")
        if isinstance(exec_ms, (int, float)) and not isinstance(exec_ms, bool) and exec_ms >= 0:
            duration = timedelta(milliseconds=exec_ms)
            if start is not None and end is None:
                end = start + duration
            elif end is not None and start is None:
                start = end - duration
        if start is not None and end is not None and end < start:
            end = start
        return _to_ns(start), _to_ns(end)

    def _parent_context(self, decision: Any) -> Any:
        """Parent for the decision span: the span that was active when the
        decision was captured (carried in the record, because background
        export runs on a worker thread that cannot see the caller's
        context), else whatever span is current here."""
        trace_id = get_field(decision, "trace_id")
        span_id = get_field(decision, "parent_span_id")
        if trace_id and span_id:
            try:
                flags = get_field(decision, "trace_flags")
                span_context = self._trace.SpanContext(
                    trace_id=int(str(trace_id), 16),
                    span_id=int(str(span_id), 16),
                    is_remote=False,
                    trace_flags=self._trace.TraceFlags(1 if flags is None else int(flags)),
                )
                if span_context.is_valid:
                    return self._trace.set_span_in_context(
                        self._trace.NonRecordingSpan(span_context)
                    )
            except (TypeError, ValueError):
                pass
        return self._trace.set_span_in_context(self._trace.get_current_span())

    # ------------------------------------------------------------------
    # BaseExporter interface
    # ------------------------------------------------------------------

    async def export(self, decision: Any) -> bool:
        """Create an OTel span for a single decision record."""
        try:
            attrs = self._extract_attrs(decision)
            start_ns, end_ns = self._span_times(decision)

            span = self._tracer.start_span(
                "briefcase.decision",
                context=self._parent_context(decision),
                kind=self._span_kind_internal,
                attributes=attrs,
                start_time=start_ns,
            )
            if get_field(decision, "error"):
                span.set_status(self._trace.Status(self._trace.StatusCode.ERROR))
            span.end(end_time=end_ns)

            return True
        except Exception:
            logger.debug("OTelExporter: could not build span", exc_info=True)
            return False

    async def flush(self) -> None:
        """Force-flush the span processor."""
        self._provider.force_flush(timeout_millis=5000)

    async def close(self) -> None:
        """Shut down the tracer provider."""
        self._provider.shutdown()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def protocol(self) -> str:
        return self._protocol

    @property
    def span_exporter(self) -> Any:
        return self._span_exporter
