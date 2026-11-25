"""Tests for briefcase/exporters/otel.py (OTelExporter).

All span-producing tests use InMemorySpanExporter; no real collector is
required. The whole module skips when opentelemetry-sdk is not installed;
the OTLP wire-exporter tests additionally skip when their protocol
package is absent.
"""

import asyncio
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402
from opentelemetry.trace import SpanKind  # noqa: E402

from briefcase.exporters.otel import OTelExporter  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal stand-in decision objects
# ---------------------------------------------------------------------------

@dataclass
class FakeModelParams:
    model_name: str = "gpt-4"
    provider: str = "openai"


@dataclass
class FakeDecision:
    decision_id: str = "dec-001"
    context_version: str = "v1.2.3"
    confidence: Optional[float] = 0.92
    model_parameters: FakeModelParams = field(default_factory=FakeModelParams)
    execution_time_ms: float = 120.5
    snapshot_type: str = "decision"
    routing_decision: Any = None
    outputs: List[Any] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_exporter(**kwargs):
    mem = InMemorySpanExporter()
    exporter = OTelExporter(
        endpoint="http://localhost:4317",
        service_name=kwargs.pop("service_name", "test-service"),
        protocol=kwargs.pop("protocol", "grpc"),
        span_exporter=mem,
        **kwargs,
    )
    return exporter, mem


def _run(coro):
    return asyncio.run(coro)


def _flush(exporter, mem):
    _run(exporter.flush())
    return mem.get_finished_spans()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_export_creates_span():
    """export() produces exactly one span named 'briefcase.decision'."""
    exp, mem = _make_exporter()
    decision = FakeDecision()
    _run(exp.export(decision))
    spans = _flush(exp, mem)
    assert len(spans) == 1
    assert spans[0].name == "briefcase.decision"


def test_span_attributes_all_present():
    """All 8 briefcase.* attributes are present on the span."""
    exp, mem = _make_exporter()
    _run(exp.export(FakeDecision()))
    spans = _flush(exp, mem)
    attrs = spans[0].attributes
    required = [
        "briefcase.context.version",
        "briefcase.decision.id",
        "briefcase.confidence",
        "briefcase.routing.action",
        "briefcase.model.name",
        "briefcase.model.provider",
        "briefcase.execution.time_ms",
        "briefcase.snapshot.type",
    ]
    for key in required:
        assert key in attrs, f"Missing attribute: {key}"


def test_span_attributes_correct_types():
    """String attributes are str; numeric attributes are float."""
    exp, mem = _make_exporter()
    _run(exp.export(FakeDecision()))
    spans = _flush(exp, mem)
    attrs = spans[0].attributes

    str_attrs = [
        "briefcase.context.version",
        "briefcase.decision.id",
        "briefcase.routing.action",
        "briefcase.model.name",
        "briefcase.model.provider",
        "briefcase.snapshot.type",
    ]
    float_attrs = ["briefcase.confidence", "briefcase.execution.time_ms"]

    for k in str_attrs:
        assert isinstance(attrs[k], str), f"{k} should be str, got {type(attrs[k])}"
    for k in float_attrs:
        assert isinstance(attrs[k], float), f"{k} should be float, got {type(attrs[k])}"


def test_trace_context_propagation():
    """When an active span exists the briefcase span is its child."""
    exp, mem = _make_exporter()

    parent_tracer = exp._provider.get_tracer("test-parent")
    with parent_tracer.start_as_current_span("parent-op") as parent_span:
        _run(exp.export(FakeDecision()))

    spans = _flush(exp, mem)
    decision_spans = [s for s in spans if s.name == "briefcase.decision"]
    assert len(decision_spans) == 1
    child = decision_spans[0]
    parent_ctx = parent_span.get_span_context()
    assert child.parent is not None
    assert child.parent.span_id == parent_ctx.span_id


def test_grpc_protocol_config():
    """protocol='grpc' creates the gRPC OTLPSpanExporter."""
    pytest.importorskip("opentelemetry.exporter.otlp.proto.grpc")
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter as GrpcExporter,
    )
    grpc_exp = OTelExporter._build_exporter("http://localhost:4317", "grpc")
    assert isinstance(grpc_exp, GrpcExporter)


def test_http_protocol_config():
    """protocol='http' creates the HTTP OTLPSpanExporter."""
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http")
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter as HttpExporter,
    )
    http_exp = OTelExporter._build_exporter("http://localhost:4318/v1/traces", "http")
    assert isinstance(http_exp, HttpExporter)


def test_export_failure_silent():
    """If attribute extraction raises, export() returns False without raising."""
    mem = MagicMock(spec=InMemorySpanExporter)
    exp = OTelExporter(span_exporter=mem)

    with patch.object(OTelExporter, "_extract_attrs", side_effect=RuntimeError("boom")):
        result = _run(exp.export(FakeDecision()))

    assert result is False


def test_service_name_in_resource():
    """The TracerProvider resource carries the configured service.name."""
    exp, _ = _make_exporter(service_name="my-agent")
    resource_attrs = exp._provider.resource.attributes
    assert resource_attrs.get("service.name") == "my-agent"


def test_multiple_decisions():
    """Exporting 5 decisions produces exactly 5 finished spans."""
    exp, mem = _make_exporter()
    for i in range(5):
        _run(exp.export(FakeDecision(decision_id=f"dec-{i:03d}")))
    spans = _flush(exp, mem)
    assert len(spans) == 5


def test_span_kind_internal():
    """Every briefcase span has SpanKind.INTERNAL."""
    exp, mem = _make_exporter()
    _run(exp.export(FakeDecision()))
    spans = _flush(exp, mem)
    assert spans[0].kind == SpanKind.INTERNAL


def test_missing_otlp_exporter():
    """ImportError with install hint when the OTLP gRPC exporter is not importable."""
    grpc_module = "opentelemetry.exporter.otlp.proto.grpc.trace_exporter"
    original = sys.modules.pop(grpc_module, None)
    try:
        with patch.dict(sys.modules, {grpc_module: None}):
            with pytest.raises(ImportError, match="pip install"):
                OTelExporter._build_exporter("http://localhost:4317", "grpc")
    finally:
        if original is not None:
            sys.modules[grpc_module] = original


def test_missing_otlp_exporter_http():
    """ImportError with install hint when the OTLP HTTP exporter is not importable."""
    http_module = "opentelemetry.exporter.otlp.proto.http.trace_exporter"
    original = sys.modules.pop(http_module, None)
    try:
        with patch.dict(sys.modules, {http_module: None}):
            with pytest.raises(ImportError, match="pip install"):
                OTelExporter._build_exporter("http://localhost:4318", "http")
    finally:
        if original is not None:
            sys.modules[http_module] = original


def test_invalid_protocol():
    """ValueError raised for an unsupported protocol string."""
    with pytest.raises(ValueError, match="Unsupported protocol"):
        OTelExporter._build_exporter("http://localhost:4317", "ftp")


def test_init_without_injected_exporter():
    """span_exporter=None builds a real OTLP exporter."""
    pytest.importorskip("opentelemetry.exporter.otlp.proto.grpc")
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter as GrpcExporter,
    )
    exp = OTelExporter(endpoint="http://localhost:4317", service_name="svc", protocol="grpc")
    assert isinstance(exp.span_exporter, GrpcExporter)
    assert exp.protocol == "grpc"


def test_protocol_property():
    """protocol property returns the configured protocol string."""
    exp, _ = _make_exporter(protocol="grpc")
    assert exp.protocol == "grpc"


def test_span_exporter_property():
    """span_exporter property returns the underlying SpanExporter."""
    mem = InMemorySpanExporter()
    exp = OTelExporter(span_exporter=mem)
    assert exp.span_exporter is mem


def test_close_shuts_down():
    """close() does not raise."""
    exp, _ = _make_exporter()
    _run(exp.close())


def test_context_object_version():
    """A context object with .version supplies briefcase.context.version."""
    class FakeCtx:
        version = "ctx-v2"

    @dataclass
    class DecisionWithCtx:
        decision_id: str = "x"
        context: Any = field(default_factory=FakeCtx)
        confidence: Optional[float] = None
        model_parameters: Any = None
        execution_time_ms: Optional[float] = None
        snapshot_type: Optional[str] = None
        routing_decision: Any = None
        outputs: List[Any] = field(default_factory=list)

    exp, mem = _make_exporter()
    _run(exp.export(DecisionWithCtx()))
    spans = _flush(exp, mem)
    assert spans[0].attributes["briefcase.context.version"] == "ctx-v2"


def test_outputs_list_confidence():
    """Confidence is read from the outputs list when decision.confidence is None."""
    class FakeOutput:
        confidence = 0.77

    @dataclass
    class DecisionListOut:
        decision_id: str = "y"
        context_version: str = ""
        outputs: List[Any] = field(default_factory=lambda: [FakeOutput()])
        confidence: Optional[float] = None
        model_parameters: Any = None
        execution_time_ms: Optional[float] = None
        snapshot_type: str = "decision"
        routing_decision: Any = None

    exp, mem = _make_exporter()
    _run(exp.export(DecisionListOut()))
    spans = _flush(exp, mem)
    assert spans[0].attributes["briefcase.confidence"] == pytest.approx(0.77)


def test_outputs_dict_confidence():
    """Confidence is read from an outputs dict."""
    @dataclass
    class DecisionDictOut:
        decision_id: str = "z"
        context_version: str = ""
        outputs: Dict[str, Any] = field(default_factory=lambda: {"confidence": 0.55})
        confidence: Optional[float] = None
        model_parameters: Any = None
        execution_time_ms: Optional[float] = None
        snapshot_type: str = "decision"
        routing_decision: Any = None

    exp, mem = _make_exporter()
    _run(exp.export(DecisionDictOut()))
    spans = _flush(exp, mem)
    assert spans[0].attributes["briefcase.confidence"] == pytest.approx(0.55)


def test_routing_decision_action():
    """briefcase.routing.action comes from the routing decision when present."""
    class FakeRouting:
        action = "human_review"

    d = FakeDecision(routing_decision=FakeRouting())
    exp, mem = _make_exporter()
    _run(exp.export(d))
    spans = _flush(exp, mem)
    assert spans[0].attributes["briefcase.routing.action"] == "human_review"


def test_model_params_dict():
    """model_parameters given as a dict is handled."""
    @dataclass
    class DecisionDictParams:
        decision_id: str = "mp"
        context_version: str = ""
        outputs: List[Any] = field(default_factory=list)
        confidence: Optional[float] = None
        model_parameters: Dict[str, str] = field(
            default_factory=lambda: {"model_name": "claude-3", "provider": "anthropic"}
        )
        execution_time_ms: Optional[float] = None
        snapshot_type: str = "decision"
        routing_decision: Any = None

    exp, mem = _make_exporter()
    _run(exp.export(DecisionDictParams()))
    spans = _flush(exp, mem)
    assert spans[0].attributes["briefcase.model.name"] == "claude-3"
    assert spans[0].attributes["briefcase.model.provider"] == "anthropic"


def test_snapshot_type_inferred_batch():
    """snapshot_type is inferred as 'batch' from the class name."""
    class BatchDecision:
        decision_id = "b"
        context_version = ""
        outputs = []
        confidence = None
        model_parameters = None
        execution_time_ms = None
        snapshot_type = None
        routing_decision = None
        context = None

    exp, mem = _make_exporter()
    _run(exp.export(BatchDecision()))
    spans = _flush(exp, mem)
    assert spans[0].attributes["briefcase.snapshot.type"] == "batch"


def test_snapshot_type_inferred_session():
    """snapshot_type is inferred as 'session' from the class name."""
    class SessionDecision:
        decision_id = "s"
        context_version = ""
        outputs = []
        confidence = None
        model_parameters = None
        execution_time_ms = None
        snapshot_type = None
        routing_decision = None
        context = None

    exp, mem = _make_exporter()
    _run(exp.export(SessionDecision()))
    spans = _flush(exp, mem)
    assert spans[0].attributes["briefcase.snapshot.type"] == "session"


def test_snapshot_type_inferred_decision():
    """snapshot_type defaults to 'decision' when the class name has no hint."""
    class AnonymousRecord:
        decision_id = "a"
        context_version = ""
        outputs = []
        confidence = None
        model_parameters = None
        execution_time_ms = None
        snapshot_type = None
        routing_decision = None
        context = None

    exp, mem = _make_exporter()
    _run(exp.export(AnonymousRecord()))
    spans = _flush(exp, mem)
    assert spans[0].attributes["briefcase.snapshot.type"] == "decision"


# ---------------------------------------------------------------------------
# Dict records (@capture), span timing, and parent context across threads
# ---------------------------------------------------------------------------

_CAPTURE_RECORD = {
    "schema_version": "1.1",
    "decision_id": "dec-dict-1",
    "decision_type": "classify",
    "function_name": "classify",
    "inputs": {"args": "('x',)"},
    "outputs": {"result": "'X'"},
    "started_at": "2026-08-15T12:00:00+00:00",
    "ended_at": "2026-08-15T12:00:00.250000+00:00",
    "execution_time_ms": 250.0,
    "context_version": "v9",
    "model_parameters": {"model_name": "m1", "provider": "p1"},
}


def test_dict_record_populates_attributes():
    """A @capture dict record is read like an object record."""
    exp, mem = _make_exporter()
    assert _run(exp.export(dict(_CAPTURE_RECORD))) is True
    attrs = _flush(exp, mem)[0].attributes
    assert attrs["briefcase.decision.id"] == "dec-dict-1"
    assert attrs["briefcase.execution.time_ms"] == 250.0
    assert attrs["briefcase.context.version"] == "v9"
    assert attrs["briefcase.model.name"] == "m1"
    assert attrs["briefcase.model.provider"] == "p1"


def test_span_uses_the_record_start_and_end_times():
    exp, mem = _make_exporter()
    _run(exp.export(dict(_CAPTURE_RECORD)))
    span = _flush(exp, mem)[0]
    assert span.start_time == 1786795200 * 10**9
    assert span.end_time - span.start_time == 250 * 10**6


def test_span_times_derive_from_timestamp_and_duration_on_objects():
    from datetime import datetime, timezone

    @dataclass
    class Timed(FakeDecision):
        timestamp: Any = None

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    exp, mem = _make_exporter()
    _run(exp.export(Timed(timestamp=start, execution_time_ms=40.0)))
    span = _flush(exp, mem)[0]
    assert span.start_time == int(start.timestamp()) * 10**9
    assert span.end_time - span.start_time == 40 * 10**6


def test_error_record_sets_error_status():
    from opentelemetry.trace import StatusCode

    exp, mem = _make_exporter()
    _run(exp.export({**_CAPTURE_RECORD, "error": "boom"}))
    assert _flush(exp, mem)[0].status.status_code == StatusCode.ERROR


def test_parent_context_survives_background_capture():
    """@capture(async_capture=True) exports on a worker thread; the decision
    span must still be a child of the span active at call time."""
    from opentelemetry.sdk.trace import TracerProvider

    from briefcase._export_mixin import wait_for_pending_exports
    from briefcase.decorators import capture

    exp, mem = _make_exporter()
    app_tracer = TracerProvider().get_tracer("app")

    @capture(exporter=exp, async_capture=True)
    def decide(x):
        return x

    with app_tracer.start_as_current_span("request") as parent:
        decide(1)
        parent_ctx = parent.get_span_context()
    assert wait_for_pending_exports(5.0)

    span = _flush(exp, mem)[0]
    assert span.attributes["briefcase.decision.id"]
    assert span.attributes["briefcase.execution.time_ms"] >= 0.0
    assert span.context.trace_id == parent_ctx.trace_id
    assert span.parent is not None
    assert span.parent.span_id == parent_ctx.span_id
    assert span.end_time >= span.start_time
