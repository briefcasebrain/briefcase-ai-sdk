"""Tests for briefcase/integrations/frameworks/openai_agents_handler.py.

The `agents` package is stubbed by conftest.py; span data objects are
MagicMocks specced against the stub classes so isinstance dispatch works.
"""

import time
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("agents")

import briefcase.integrations.frameworks.openai_agents_handler as _mod  # noqa: E402
from briefcase._export_mixin import wait_for_pending_exports  # noqa: E402
from briefcase.integrations.frameworks.openai_agents_handler import (  # noqa: E402
    OpenAIAgentsTracer,
    install,
)
from briefcase.config import BriefcaseConfig  # noqa: E402
from briefcase.config import setup as briefcase_setup  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_span_data(kind: str) -> Any:
    """Return a mock span_data object matching the given kind."""
    from agents import (
        AgentSpanData,
        FunctionSpanData,
        HandoffSpanData,
        GuardrailSpanData,
        GenerationSpanData,
    )
    if kind == "agent":
        sd = MagicMock(spec=AgentSpanData)
        sd.name = "TestAgent"
        sd.tools = ["tool_a", "tool_b"]
        sd.handoffs = []
        sd.output_type = "text"
        return sd
    if kind == "function":
        sd = MagicMock(spec=FunctionSpanData)
        sd.name = "my_tool"
        sd.input = '{"x": 1}'
        sd.output = '{"result": 42}'
        return sd
    if kind == "handoff":
        sd = MagicMock(spec=HandoffSpanData)
        sd.from_agent = "AgentA"
        sd.to_agent = "AgentB"
        return sd
    if kind == "guardrail":
        sd = MagicMock(spec=GuardrailSpanData)
        sd.name = "PII-guard"
        sd.triggered = True
        return sd
    if kind == "generation":
        sd = MagicMock(spec=GenerationSpanData)
        sd.model = "gpt-4o"
        sd.usage = {"prompt_tokens": 10, "completion_tokens": 5}
        sd.input = [{"role": "user", "content": "hi"}]
        sd.output = [{"role": "assistant", "content": "hello"}]
        return sd
    raise ValueError(kind)


def _make_span(kind: str, span_id: str = "span-1", trace_id: str = "trace-1") -> Any:
    span = MagicMock()
    span.span_id = span_id
    span.trace_id = trace_id
    span.span_data = _make_span_data(kind)
    span.error = None
    return span


def _make_trace(trace_id: str = "trace-1", name: str = "test-trace") -> Any:
    trace = MagicMock()
    trace.trace_id = trace_id
    trace.name = name
    return trace


def _run_trace(tracer: OpenAIAgentsTracer, trace_id: str, spans: list) -> None:
    """Simulate a complete trace lifecycle."""
    t = _make_trace(trace_id)
    tracer.on_trace_start(t)
    for kind, span_id in spans:
        s = _make_span(kind, span_id, trace_id)
        tracer.on_span_start(s)
        tracer.on_span_end(s)
    tracer.on_trace_end(t)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_state():
    """Reset the global installer and BriefcaseConfig around each test."""
    _mod._INSTALLED_PROCESSOR = None
    BriefcaseConfig.reset()
    yield
    _mod._INSTALLED_PROCESSOR = None
    BriefcaseConfig.reset()


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------

def test_tracer_captures_agent_run():
    """AgentSpanData is captured as type='agent_run'."""
    tracer = OpenAIAgentsTracer()
    _run_trace(tracer, "t1", [("agent", "s1")])

    records = tracer.get_records()
    assert len(records) == 1
    spans = records[0]["spans"]
    assert len(spans) == 1
    span = spans[0]
    assert span["type"] == "agent_run"
    assert span["agent_name"] == "TestAgent"
    assert "tool_a" in span["tools"]
    assert span["output_type"] == "text"


def test_trace_record_uses_wire_field_names():
    """Trace records carry the decision-record core field names."""
    tracer = OpenAIAgentsTracer()
    _run_trace(tracer, "t1", [("agent", "s1")])

    record = tracer.get_records()[0]
    for key in ("decision_id", "decision_type", "function_name", "inputs",
                "outputs", "started_at", "ended_at", "execution_time_ms"):
        assert key in record, key
    assert record["decision_type"] == "agent_trace"
    assert record["decision_id"] == "t1"


def test_tracer_captures_tool_calls():
    """FunctionSpanData is captured as type='tool_call'."""
    tracer = OpenAIAgentsTracer()
    _run_trace(tracer, "t1", [("function", "s1")])

    records = tracer.get_records()
    span = records[0]["spans"][0]
    assert span["type"] == "tool_call"
    assert span["tool_name"] == "my_tool"
    assert span["input"] == '{"x": 1}'
    assert span["output"] == '{"result": 42}'


def test_tracer_captures_handoffs():
    """HandoffSpanData is captured as type='handoff'."""
    tracer = OpenAIAgentsTracer()
    _run_trace(tracer, "t1", [("handoff", "s1")])

    span = tracer.get_records()[0]["spans"][0]
    assert span["type"] == "handoff"
    assert span["from_agent"] == "AgentA"
    assert span["to_agent"] == "AgentB"


def test_tracer_captures_guardrails():
    """GuardrailSpanData is captured as type='guardrail'."""
    tracer = OpenAIAgentsTracer()
    _run_trace(tracer, "t1", [("guardrail", "s1")])

    span = tracer.get_records()[0]["spans"][0]
    assert span["type"] == "guardrail"
    assert span["guardrail_name"] == "PII-guard"
    assert span["triggered"] is True


def test_tracer_async_no_block():
    """async_capture=True returns without blocking on the exporter."""
    calls = []

    class SlowExporter:
        async def export(self, record):
            time.sleep(0.05)
            calls.append(record)

        async def flush(self):
            pass

        async def close(self):
            pass

    briefcase_setup(exporter=SlowExporter())
    tracer = OpenAIAgentsTracer(async_capture=True)

    start = time.monotonic()
    _run_trace(tracer, "t1", [("agent", "s1")])
    elapsed = time.monotonic() - start

    # Returns quickly (well under 50 ms)
    assert elapsed < 0.04, f"Blocked for {elapsed:.3f}s"

    assert wait_for_pending_exports(5.0)
    assert len(calls) == 1


def test_tracer_capture_failure_silent():
    """Errors in on_span_end / on_trace_end never propagate to the caller."""
    tracer = OpenAIAgentsTracer()

    bad_span = MagicMock()
    bad_span.span_id = object()
    bad_span.trace_id = "t99"
    bad_span.span_data = None
    bad_span.error = None

    # Must not raise
    tracer.on_span_start(bad_span)
    tracer.on_span_end(bad_span)

    bad_trace = MagicMock()
    bad_trace.trace_id = object()
    tracer.on_trace_end(bad_trace)


def test_tracer_missing_dependency():
    """Instantiating the tracer when openai-agents is absent raises."""
    original = _mod._AGENTS_AVAILABLE
    try:
        _mod._AGENTS_AVAILABLE = False
        with pytest.raises(ImportError, match="pip install"):
            OpenAIAgentsTracer()
    finally:
        _mod._AGENTS_AVAILABLE = original


def test_tracer_links_context_version():
    """context_version appears on every trace record."""
    tracer = OpenAIAgentsTracer(context_version="v3.1")
    _run_trace(tracer, "t1", [("agent", "s1")])

    record = tracer.get_records()[0]
    assert record["context_version"] == "v3.1"


def test_install_patches_globally():
    """install() registers the tracer with the agents tracing layer."""
    with patch.object(_mod, "_agents_add_trace_processor") as mock_add:
        tracer = install()
        assert mock_add.called
        assert isinstance(tracer, OpenAIAgentsTracer)
        assert _mod._INSTALLED_PROCESSOR is tracer


def test_install_idempotent():
    """Calling install() twice returns the same instance without re-registering."""
    with patch.object(_mod, "_agents_add_trace_processor") as mock_add:
        t1 = install()
        t2 = install()
        assert t1 is t2
        assert mock_add.call_count == 1  # registered only once


# ---------------------------------------------------------------------------
# Additional coverage tests
# ---------------------------------------------------------------------------

def test_tracer_captures_generation():
    """GenerationSpanData is captured as type='generation'."""
    tracer = OpenAIAgentsTracer()
    _run_trace(tracer, "t1", [("generation", "s1")])

    span = tracer.get_records()[0]["spans"][0]
    assert span["type"] == "generation"
    assert span["model"] == "gpt-4o"
    assert span["usage"]["prompt_tokens"] == 10


def test_tracer_execution_time_ms_populated():
    """Span records include execution_time_ms when the start time is known."""
    tracer = OpenAIAgentsTracer()
    _run_trace(tracer, "t1", [("agent", "s1")])
    span = tracer.get_records()[0]["spans"][0]
    assert "execution_time_ms" in span
    assert span["execution_time_ms"] >= 0


def test_tracer_span_error_captured():
    """Span errors are recorded in the span record."""
    tracer = OpenAIAgentsTracer()
    t = _make_trace("t1")
    tracer.on_trace_start(t)

    s = _make_span("function", "s1", "t1")
    s.error = Exception("something broke")
    tracer.on_span_start(s)
    tracer.on_span_end(s)

    tracer.on_trace_end(t)
    span = tracer.get_records()[0]["spans"][0]
    assert "error" in span
    assert "something broke" in span["error"]


def test_tracer_multiple_spans_in_trace():
    """All spans within a trace appear in the record."""
    tracer = OpenAIAgentsTracer()
    _run_trace(
        tracer, "t1",
        [("agent", "s1"), ("function", "s2"), ("handoff", "s3")]
    )
    spans = tracer.get_records()[0]["spans"]
    assert len(spans) == 3
    types = {s["type"] for s in spans}
    assert types == {"agent_run", "tool_call", "handoff"}


def test_tracer_multiple_concurrent_traces():
    """Interleaved traces stay isolated from each other."""
    tracer = OpenAIAgentsTracer()

    t1 = _make_trace("trace-A", "alpha")
    t2 = _make_trace("trace-B", "beta")

    tracer.on_trace_start(t1)
    tracer.on_trace_start(t2)

    s1 = _make_span("agent", "s-A1", "trace-A")
    s2 = _make_span("function", "s-B1", "trace-B")
    tracer.on_span_start(s1)
    tracer.on_span_start(s2)
    tracer.on_span_end(s1)
    tracer.on_span_end(s2)

    tracer.on_trace_end(t1)
    tracer.on_trace_end(t2)

    records = tracer.get_records()
    assert len(records) == 2
    alpha = next(r for r in records if r["name"] == "alpha")
    beta = next(r for r in records if r["name"] == "beta")
    assert alpha["spans"][0]["type"] == "agent_run"
    assert beta["spans"][0]["type"] == "tool_call"


def test_tracer_trace_timestamps():
    """Trace records have started_at and ended_at ISO strings."""
    tracer = OpenAIAgentsTracer()
    _run_trace(tracer, "t1", [])
    record = tracer.get_records()[0]
    assert "started_at" in record
    assert "ended_at" in record
    # They parse as ISO datetimes
    datetime.fromisoformat(record["started_at"])
    datetime.fromisoformat(record["ended_at"])


def test_tracer_clear():
    """clear() resets all captured records and in-flight state."""
    tracer = OpenAIAgentsTracer()
    _run_trace(tracer, "t1", [("agent", "s1")])
    assert len(tracer.get_records()) == 1

    # Start a trace but do not end it
    t = _make_trace("t2")
    tracer.on_trace_start(t)

    tracer.clear()
    assert tracer.get_records() == []
    assert tracer._traces == {}
    assert tracer._span_starts == {}
    assert tracer._trace_starts == {}


def test_tracer_shutdown():
    """shutdown() clears in-flight state without raising."""
    tracer = OpenAIAgentsTracer()
    t = _make_trace("t1")
    tracer.on_trace_start(t)
    s = _make_span("agent", "s1", "t1")
    tracer.on_span_start(s)

    tracer.shutdown()
    assert tracer._traces == {}
    assert tracer._span_starts == {}


def test_tracer_force_flush():
    """force_flush() does not raise."""
    tracer = OpenAIAgentsTracer()
    tracer.force_flush()  # no-op


def test_tracer_get_records_returns_copy():
    """get_records() returns a copy, not the internal list."""
    tracer = OpenAIAgentsTracer()
    _run_trace(tracer, "t1", [])
    r1 = tracer.get_records()
    r1.clear()
    r2 = tracer.get_records()
    assert len(r2) == 1


def test_tracer_sync_export():
    """async_capture=False calls export synchronously."""
    calls = []

    class SyncExporter:
        async def export(self, record):
            calls.append(record)

        async def flush(self):
            pass

        async def close(self):
            pass

    briefcase_setup(exporter=SyncExporter())
    tracer = OpenAIAgentsTracer(async_capture=False)
    _run_trace(tracer, "t1", [("agent", "s1")])

    # Synchronous path: the call completes immediately
    assert len(calls) == 1


def test_tracer_no_exporter_configured():
    """When no exporter is configured, _trigger_export is a no-op."""
    tracer = OpenAIAgentsTracer()
    record = {"trace_id": "t1", "spans": []}
    tracer._trigger_export(record)  # must not raise


def test_tracer_export_exception_silent():
    """Exporter errors in _trigger_export never propagate."""
    class BrokenExporter:
        async def export(self, record):
            raise RuntimeError("export failed")

        async def flush(self):
            pass

        async def close(self):
            pass

    briefcase_setup(exporter=BrokenExporter())
    tracer = OpenAIAgentsTracer(async_capture=False)
    # Must not raise
    tracer._trigger_export({"trace_id": "t1", "spans": []})


def test_tracer_on_trace_end_missing_trace():
    """on_trace_end for an unknown trace_id is a no-op."""
    tracer = OpenAIAgentsTracer()
    t = _make_trace("non-existent")
    tracer.on_trace_end(t)  # never started; must not raise
    assert tracer.get_records() == []


def test_tracer_span_no_start_time():
    """Without on_span_start, started_at is None in the span record."""
    tracer = OpenAIAgentsTracer()
    t = _make_trace("t1")
    tracer.on_trace_start(t)

    # Call on_span_end without on_span_start
    s = _make_span("agent", "s1", "t1")
    tracer.on_span_end(s)

    tracer.on_trace_end(t)
    span = tracer.get_records()[0]["spans"][0]
    assert span["started_at"] is None
    assert "execution_time_ms" not in span


def test_tracer_unknown_span_type():
    """An unknown span_data type takes its type from span_data.type."""
    tracer = OpenAIAgentsTracer()
    t = _make_trace("t1")
    tracer.on_trace_start(t)

    s = MagicMock()
    s.span_id = "s1"
    s.trace_id = "t1"
    s.span_data = MagicMock()
    s.span_data.type = "custom_type"
    # Ensure it is NOT an instance of any known span data class
    s.span_data.__class__ = type("UnknownSpanData", (), {})
    s.error = None
    tracer.on_span_start(s)
    tracer.on_span_end(s)
    tracer.on_trace_end(t)

    span = tracer.get_records()[0]["spans"][0]
    assert span["type"] == "custom_type"


def test_install_missing_dependency():
    """install() raises ImportError when openai-agents is absent."""
    original = _mod._AGENTS_AVAILABLE
    try:
        _mod._AGENTS_AVAILABLE = False
        with pytest.raises(ImportError, match="pip install"):
            install()
    finally:
        _mod._AGENTS_AVAILABLE = original


def test_install_returns_tracer_with_context_version():
    """install() passes context_version to the tracer."""
    with patch.object(_mod, "_agents_add_trace_processor"):
        tracer = install(context_version="v9.9")
        assert tracer.context_version == "v9.9"


def test_tracer_no_context_version():
    """Trace records do NOT include context_version when not set."""
    tracer = OpenAIAgentsTracer()
    _run_trace(tracer, "t1", [])
    record = tracer.get_records()[0]
    assert "context_version" not in record


def test_tracer_async_background_export_exception_silent():
    """Background export errors never propagate."""
    class FailingExporter:
        async def export(self, record):
            raise RuntimeError("bg fail")

        async def flush(self):
            pass

        async def close(self):
            pass

    briefcase_setup(exporter=FailingExporter())
    tracer = OpenAIAgentsTracer(async_capture=True)
    _run_trace(tracer, "t1", [("agent", "s1")])
    assert wait_for_pending_exports(5.0)
    # Test passes when no exception propagated


def test_on_trace_start_exception_silent():
    """A malformed trace object in on_trace_start does not raise."""
    tracer = OpenAIAgentsTracer()
    # An object with no trace_id attribute triggers AttributeError internally
    tracer.on_trace_start(object())  # must not raise
