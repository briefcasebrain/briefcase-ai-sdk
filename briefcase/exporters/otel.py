"""OpenTelemetry exporter for decision records.

Translates decision records into OpenTelemetry spans and ships them to an
OTLP collector. Each exported record becomes one ``briefcase.decision``
span carrying the ``briefcase.*`` attributes; when a span is already
active, the decision span is created as its child so trace context
propagates.

Install
-------
Requires ``opentelemetry-api`` and ``opentelemetry-sdk``, plus the OTLP
exporter package for the chosen protocol:
``pip install briefcase-ai[otel]``. The imports are lazy, so this module
loads without OpenTelemetry installed and fails with a clear error on
construction.
"""

from __future__ import annotations

from typing import Any, Optional

from briefcase.exporters.base import BaseExporter


_OTEL_INSTALL_HINT = (
    "opentelemetry-api and opentelemetry-sdk are required. Install with "
    "'pip install briefcase-ai[otel]'."
)


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
        """Pull the eight required briefcase.* attributes from a decision object."""
        attrs: dict = {}

        # briefcase.decision.id
        decision_id = (
            getattr(decision, "decision_id", None)
            or getattr(decision, "id", None)
            or ""
        )
        attrs["briefcase.decision.id"] = str(decision_id)

        # briefcase.context.version
        context_version = ""
        ctx = getattr(decision, "context", None)
        if ctx is not None:
            context_version = str(getattr(ctx, "version", "") or "")
        if not context_version:
            context_version = str(getattr(decision, "context_version", "") or "")
        attrs["briefcase.context.version"] = context_version

        # briefcase.confidence
        confidence: float = 0.0
        outputs = getattr(decision, "outputs", None)
        if outputs:
            if isinstance(outputs, list):
                for out in outputs:
                    c = getattr(out, "confidence", None)
                    if c is not None:
                        confidence = float(c)
                        break
            elif isinstance(outputs, dict):
                confidence = float(outputs.get("confidence", 0.0))
        conf_direct = getattr(decision, "confidence", None)
        if conf_direct is not None:
            confidence = float(conf_direct)
        attrs["briefcase.confidence"] = confidence

        # briefcase.routing.action
        routing = getattr(decision, "routing_decision", None)
        if routing is not None:
            attrs["briefcase.routing.action"] = str(getattr(routing, "action", "unrouted"))
        else:
            attrs["briefcase.routing.action"] = "unrouted"

        # briefcase.model.name / briefcase.model.provider
        model_name = ""
        model_provider = ""
        model_params = getattr(decision, "model_parameters", None)
        if model_params is not None:
            if isinstance(model_params, dict):
                model_name = str(model_params.get("model_name", "") or "")
                model_provider = str(model_params.get("provider", "") or "")
            else:
                model_name = str(getattr(model_params, "model_name", "") or "")
                model_provider = str(getattr(model_params, "provider", "") or "")
        attrs["briefcase.model.name"] = model_name
        attrs["briefcase.model.provider"] = model_provider

        # briefcase.execution.time_ms
        exec_ms = getattr(decision, "execution_time_ms", None)
        attrs["briefcase.execution.time_ms"] = float(exec_ms) if exec_ms is not None else 0.0

        # briefcase.snapshot.type, inferred from the class name when absent
        snapshot_type = getattr(decision, "snapshot_type", None)
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

    # ------------------------------------------------------------------
    # BaseExporter interface
    # ------------------------------------------------------------------

    async def export(self, decision: Any) -> bool:
        """Create an OTel span for a single decision record."""
        try:
            attrs = self._extract_attrs(decision)

            # W3C trace context propagation: child of the active span if one exists.
            current_span = self._trace.get_current_span()
            ctx = self._trace.set_span_in_context(current_span)

            with self._tracer.start_as_current_span(
                "briefcase.decision",
                context=ctx,
                kind=self._span_kind_internal,
                attributes=attrs,
            ):
                pass  # span recorded on exit

            return True
        except Exception:
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
