"""Tests for briefcase/integrations/frameworks/crewai_handler.py.

crewai is stubbed by conftest.py; a mock event bus stores registered
handlers so tests can fire events directly.
"""

import time
from typing import Any
from unittest.mock import MagicMock

import pytest

import briefcase.integrations.frameworks.crewai_handler as _mod
from briefcase._export_mixin import wait_for_pending_exports
from briefcase.integrations.frameworks.crewai_handler import (
    CrewAIEventListener,
    require_crewai,
)
from briefcase.config import BriefcaseConfig
from briefcase.config import setup as briefcase_setup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_bus():
    """Return a mock event bus that stores registered handlers."""
    bus = MagicMock()
    _handlers: dict = {}

    def _on(event_cls):
        def _decorator(fn):
            key = getattr(event_cls, "__name__", str(event_cls))
            _handlers.setdefault(key, []).append(fn)
            return fn
        return _decorator

    bus.on = _on
    bus._handlers = _handlers
    return bus


def _make_crew_event(event_type: str, **attrs) -> Any:
    """Create a mock crew event of the given type name with given attributes."""
    event = MagicMock()
    for k, v in attrs.items():
        setattr(event, k, v)
    type(event).__name__ = event_type
    return event


def _fire_on_bus(bus: MagicMock, event_cls_name: str, event: Any) -> None:
    """Fire all handlers registered for event_cls_name on the bus."""
    handlers = bus._handlers.get(event_cls_name, [])
    for h in handlers:
        h(event)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def ensure_crewai_available(monkeypatch):
    """Enable the CrewAI handler without requiring crewai to be installed."""
    monkeypatch.setattr(_mod, "_CREWAI_AVAILABLE", True)
    for cls_name in [
        "CrewKickoffStartedEvent", "CrewKickoffCompletedEvent", "CrewKickoffFailedEvent",
        "AgentExecutionStartedEvent", "AgentExecutionCompletedEvent", "AgentExecutionErrorEvent",
        "TaskStartedEvent", "TaskCompletedEvent", "TaskFailedEvent",
        "ToolUsageStartedEvent", "ToolUsageFinishedEvent", "ToolUsageErrorEvent",
        "LLMCallStartedEvent", "LLMCallCompletedEvent", "LLMCallFailedEvent",
    ]:
        monkeypatch.setattr(_mod, cls_name, type(cls_name, (), {}))


@pytest.fixture(autouse=True)
def reset_config():
    BriefcaseConfig.reset()
    yield
    BriefcaseConfig.reset()


@pytest.fixture
def mock_bus():
    return _make_mock_bus()


def _make_listener_with_mock_bus(mock_bus, **kwargs):
    """Create a CrewAIEventListener, bypassing auto-registration, then wire the mock bus."""
    original = _mod._crewai_bus
    _mod._crewai_bus = None
    try:
        listener = CrewAIEventListener(**kwargs)
    finally:
        _mod._crewai_bus = original
    listener.setup_listeners(mock_bus)
    return listener


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------

def test_crew_kickoff_captured(mock_bus):
    """CrewKickoffStartedEvent + Completed produce a crew_kickoff record."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("CrewKickoffStartedEvent", crew_id="crew1", name="MyCrew")
    complete = _make_crew_event("CrewKickoffCompletedEvent", crew_id="crew1", output="done")

    _fire_on_bus(mock_bus, "CrewKickoffStartedEvent", start)
    _fire_on_bus(mock_bus, "CrewKickoffCompletedEvent", complete)

    records = listener.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "crew_kickoff"
    assert "execution_time_ms" in records[0]


def test_paired_record_uses_wire_field_names(mock_bus):
    """A paired record carries the decision-record core field names."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("CrewKickoffStartedEvent", crew_id="crew1")
    complete = _make_crew_event("CrewKickoffCompletedEvent", crew_id="crew1")
    _fire_on_bus(mock_bus, "CrewKickoffStartedEvent", start)
    _fire_on_bus(mock_bus, "CrewKickoffCompletedEvent", complete)

    record = listener.get_records()[0]
    for key in ("decision_id", "decision_type", "function_name", "inputs",
                "outputs", "started_at", "ended_at", "execution_time_ms"):
        assert key in record, key
    assert record["function_name"] == "crew1"


def test_agent_execution_captured(mock_bus):
    """AgentExecutionStartedEvent + Completed produce an agent_execution record."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("AgentExecutionStartedEvent", agent_id="a1", role="Researcher")
    complete = _make_crew_event("AgentExecutionCompletedEvent", agent_id="a1", output="research done")

    _fire_on_bus(mock_bus, "AgentExecutionStartedEvent", start)
    _fire_on_bus(mock_bus, "AgentExecutionCompletedEvent", complete)

    records = listener.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "agent_execution"


def test_task_lifecycle_captured(mock_bus):
    """TaskStartedEvent + Completed produce a task record."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("TaskStartedEvent", task_id="t1", description="Write report")
    complete = _make_crew_event("TaskCompletedEvent", task_id="t1", output="report text")

    _fire_on_bus(mock_bus, "TaskStartedEvent", start)
    _fire_on_bus(mock_bus, "TaskCompletedEvent", complete)

    records = listener.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "task"


def test_tool_usage_captured(mock_bus):
    """ToolUsageStartedEvent + FinishedEvent produce a tool_usage record."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("ToolUsageStartedEvent", tool_name="web_search")
    finish = _make_crew_event("ToolUsageFinishedEvent", tool_name="web_search", result="results")

    _fire_on_bus(mock_bus, "ToolUsageStartedEvent", start)
    _fire_on_bus(mock_bus, "ToolUsageFinishedEvent", finish)

    records = listener.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "tool_usage"


def test_llm_call_captured(mock_bus):
    """LLMCallStartedEvent + CompletedEvent produce an llm_call record."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("LLMCallStartedEvent", model="gpt-4o", call_id="llm1")
    complete = _make_crew_event("LLMCallCompletedEvent", call_id="llm1", output="response text")

    _fire_on_bus(mock_bus, "LLMCallStartedEvent", start)
    _fire_on_bus(mock_bus, "LLMCallCompletedEvent", complete)

    records = listener.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "llm_call"


def test_async_non_blocking(mock_bus):
    """async_capture=True returns without blocking on the exporter."""
    calls = []

    class SlowExporter:
        async def export(self, record):
            time.sleep(0.3)
            calls.append(record)

        async def flush(self):
            pass

        async def close(self):
            pass

    briefcase_setup(exporter=SlowExporter())
    listener = _make_listener_with_mock_bus(mock_bus, async_capture=True)
    assert listener.async_capture is True

    start = _make_crew_event("CrewKickoffStartedEvent", crew_id="cx", name="X")
    complete = _make_crew_event("CrewKickoffCompletedEvent", crew_id="cx")

    t_start = time.monotonic()
    _fire_on_bus(mock_bus, "CrewKickoffStartedEvent", start)
    _fire_on_bus(mock_bus, "CrewKickoffCompletedEvent", complete)
    elapsed = time.monotonic() - t_start

    assert elapsed < 0.25, f"Blocked for {elapsed:.3f}s"
    assert wait_for_pending_exports(5.0)
    assert len(calls) == 1


def test_error_event_silent(mock_bus):
    """Broken events never raise into the caller."""
    listener = _make_listener_with_mock_bus(mock_bus)
    assert listener is not None

    # Fire with a completely broken event
    _fire_on_bus(mock_bus, "CrewKickoffStartedEvent", None)
    _fire_on_bus(mock_bus, "CrewKickoffCompletedEvent", None)
    # Test passes when no exception is raised


def test_missing_dependency_raises():
    """Instantiating the listener when crewai is absent raises ImportError."""
    original = _mod._CREWAI_AVAILABLE
    try:
        _mod._CREWAI_AVAILABLE = False
        with pytest.raises(ImportError, match="pip install"):
            CrewAIEventListener()
    finally:
        _mod._CREWAI_AVAILABLE = original


def test_context_version_in_records(mock_bus):
    """context_version appears on captured records."""
    listener = _make_listener_with_mock_bus(mock_bus, context_version="v7.3")

    start = _make_crew_event("CrewKickoffStartedEvent", crew_id="c1")
    complete = _make_crew_event("CrewKickoffCompletedEvent", crew_id="c1")
    _fire_on_bus(mock_bus, "CrewKickoffStartedEvent", start)
    _fire_on_bus(mock_bus, "CrewKickoffCompletedEvent", complete)

    records = listener.get_records()
    assert len(records) == 1
    assert records[0]["context_version"] == "v7.3"


def test_exporter_integration(mock_bus):
    """Records pass to the configured exporter."""
    calls = []

    class RecordingExporter:
        async def export(self, record):
            calls.append(record)

        async def flush(self):
            pass

        async def close(self):
            pass

    briefcase_setup(exporter=RecordingExporter())
    listener = _make_listener_with_mock_bus(mock_bus, async_capture=False)
    assert listener is not None

    start = _make_crew_event("CrewKickoffStartedEvent", crew_id="c1")
    complete = _make_crew_event("CrewKickoffCompletedEvent", crew_id="c1")
    _fire_on_bus(mock_bus, "CrewKickoffStartedEvent", start)
    _fire_on_bus(mock_bus, "CrewKickoffCompletedEvent", complete)

    assert len(calls) == 1
    assert calls[0]["decision_type"] == "crew_kickoff"


def test_error_events_recorded(mock_bus):
    """Failed events are captured with an error field."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("TaskStartedEvent", task_id="t1")
    fail = _make_crew_event("TaskFailedEvent", task_id="t1", error="timeout")

    _fire_on_bus(mock_bus, "TaskStartedEvent", start)
    _fire_on_bus(mock_bus, "TaskFailedEvent", fail)

    records = listener.get_records()
    assert len(records) == 1
    assert "error" in records[0]


# ---------------------------------------------------------------------------
# Additional coverage tests
# ---------------------------------------------------------------------------

def test_clear_resets_records(mock_bus):
    """clear() resets all captured records and inflight state."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("CrewKickoffStartedEvent", crew_id="c1")
    complete = _make_crew_event("CrewKickoffCompletedEvent", crew_id="c1")
    _fire_on_bus(mock_bus, "CrewKickoffStartedEvent", start)
    _fire_on_bus(mock_bus, "CrewKickoffCompletedEvent", complete)
    assert listener.decision_count == 1

    listener.clear()
    assert listener.decision_count == 0
    assert listener._inflight == {}


def test_get_records_returns_copy(mock_bus):
    """get_records() returns a copy."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("CrewKickoffStartedEvent", crew_id="c1")
    complete = _make_crew_event("CrewKickoffCompletedEvent", crew_id="c1")
    _fire_on_bus(mock_bus, "CrewKickoffStartedEvent", start)
    _fire_on_bus(mock_bus, "CrewKickoffCompletedEvent", complete)

    r1 = listener.get_records()
    r1.clear()
    r2 = listener.get_records()
    assert len(r2) == 1


def test_decision_count_property(mock_bus):
    """decision_count reflects the captured record count."""
    listener = _make_listener_with_mock_bus(mock_bus)
    assert listener.decision_count == 0

    start = _make_crew_event("TaskStartedEvent", task_id="t1")
    complete = _make_crew_event("TaskCompletedEvent", task_id="t1")
    _fire_on_bus(mock_bus, "TaskStartedEvent", start)
    _fire_on_bus(mock_bus, "TaskCompletedEvent", complete)
    assert listener.decision_count == 1


def test_capture_crews_false(mock_bus):
    """With capture_crews=False, crew events are not registered."""
    _make_listener_with_mock_bus(mock_bus, capture_crews=False)
    assert "CrewKickoffStartedEvent" not in mock_bus._handlers


def test_capture_agents_false(mock_bus):
    """With capture_agents=False, agent events are not registered."""
    _make_listener_with_mock_bus(mock_bus, capture_agents=False)
    assert "AgentExecutionStartedEvent" not in mock_bus._handlers


def test_capture_tasks_false(mock_bus):
    """With capture_tasks=False, task events are not registered."""
    _make_listener_with_mock_bus(mock_bus, capture_tasks=False)
    assert "TaskStartedEvent" not in mock_bus._handlers


def test_capture_tools_false(mock_bus):
    """With capture_tools=False, tool events are not registered."""
    _make_listener_with_mock_bus(mock_bus, capture_tools=False)
    assert "ToolUsageStartedEvent" not in mock_bus._handlers


def test_truncation_applied(mock_bus):
    """Long inputs truncate to max_input_chars."""
    listener = _make_listener_with_mock_bus(mock_bus, max_input_chars=20)

    long_name = "t" * 5000
    start = _make_crew_event("TaskStartedEvent", task_id="t1", description=long_name)
    complete = _make_crew_event("TaskCompletedEvent", task_id="t1")
    _fire_on_bus(mock_bus, "TaskStartedEvent", start)
    _fire_on_bus(mock_bus, "TaskCompletedEvent", complete)

    records = listener.get_records()
    assert records[0]["inputs"].get("description", "")[:21] != long_name[:21]


def test_execution_time_ms_populated(mock_bus):
    """Paired start/complete events include execution_time_ms."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("LLMCallStartedEvent", call_id="llm1", model="gpt-4o")
    complete = _make_crew_event("LLMCallCompletedEvent", call_id="llm1")
    _fire_on_bus(mock_bus, "LLMCallStartedEvent", start)
    _fire_on_bus(mock_bus, "LLMCallCompletedEvent", complete)

    records = listener.get_records()
    assert "execution_time_ms" in records[0]
    assert records[0]["execution_time_ms"] >= 0


def test_sync_export(mock_bus):
    """async_capture=False exports synchronously."""
    calls = []

    class SyncExporter:
        async def export(self, record):
            calls.append(record)

        async def flush(self):
            pass

        async def close(self):
            pass

    briefcase_setup(exporter=SyncExporter())
    _make_listener_with_mock_bus(mock_bus, async_capture=False)

    start = _make_crew_event("CrewKickoffStartedEvent", crew_id="c1")
    complete = _make_crew_event("CrewKickoffCompletedEvent", crew_id="c1")
    _fire_on_bus(mock_bus, "CrewKickoffStartedEvent", start)
    _fire_on_bus(mock_bus, "CrewKickoffCompletedEvent", complete)

    assert len(calls) == 1


def test_no_exporter_configured(mock_bus):
    """When no exporter is configured, _trigger_export is a no-op."""
    listener = _make_listener_with_mock_bus(mock_bus)
    # No exporter: must not raise
    listener._trigger_export({"decision_id": "x", "decision_type": "test"})


def test_export_failure_silent(mock_bus):
    """Exporter errors never propagate."""

    class BrokenExporter:
        async def export(self, record):
            raise RuntimeError("export failed")

        async def flush(self):
            pass

        async def close(self):
            pass

    briefcase_setup(exporter=BrokenExporter())
    listener = _make_listener_with_mock_bus(mock_bus, async_capture=False)
    listener._trigger_export({"decision_id": "x", "decision_type": "test"})


def test_tool_error_captured(mock_bus):
    """ToolUsageErrorEvent produces a tool_usage record with an error field."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("ToolUsageStartedEvent", tool_name="search")
    error = _make_crew_event("ToolUsageErrorEvent", tool_name="search", error="timeout")

    _fire_on_bus(mock_bus, "ToolUsageStartedEvent", start)
    _fire_on_bus(mock_bus, "ToolUsageErrorEvent", error)

    records = listener.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "tool_usage"
    assert "error" in records[0]


def test_llm_failed_captured(mock_bus):
    """LLMCallFailedEvent produces an llm_call record with an error field."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("LLMCallStartedEvent", call_id="llm1", model="gpt-4o")
    fail = _make_crew_event("LLMCallFailedEvent", call_id="llm1", error="rate limit")

    _fire_on_bus(mock_bus, "LLMCallStartedEvent", start)
    _fire_on_bus(mock_bus, "LLMCallFailedEvent", fail)

    records = listener.get_records()
    assert len(records) == 1
    assert "error" in records[0]


def test_no_context_version(mock_bus):
    """Records do NOT include context_version when not set."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("CrewKickoffStartedEvent", crew_id="c1")
    complete = _make_crew_event("CrewKickoffCompletedEvent", crew_id="c1")
    _fire_on_bus(mock_bus, "CrewKickoffStartedEvent", start)
    _fire_on_bus(mock_bus, "CrewKickoffCompletedEvent", complete)

    records = listener.get_records()
    assert "context_version" not in records[0]


def test_require_crewai_raises_when_unavailable():
    """require_crewai() raises ImportError when crewai is absent."""
    original = _mod._CREWAI_AVAILABLE
    try:
        _mod._CREWAI_AVAILABLE = False
        with pytest.raises(ImportError, match="pip install"):
            require_crewai()
    finally:
        _mod._CREWAI_AVAILABLE = original


def test_complete_without_start_produces_record(mock_bus):
    """A completed event without a matching start still produces a record."""
    listener = _make_listener_with_mock_bus(mock_bus)

    # No start event fired; complete alone
    complete = _make_crew_event("CrewKickoffCompletedEvent", crew_id="c_orphan")
    _fire_on_bus(mock_bus, "CrewKickoffCompletedEvent", complete)

    records = listener.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "crew_kickoff"
    # No execution_time_ms without a start event: the duration is unknown
    assert "execution_time_ms" not in records[0]


# ---------------------------------------------------------------------------
# setup_listeners when unavailable
# ---------------------------------------------------------------------------

def test_setup_listeners_when_unavailable_is_noop(mock_bus, monkeypatch):
    """setup_listeners returns immediately when crewai is unavailable."""
    listener = _make_listener_with_mock_bus(mock_bus)
    monkeypatch.setattr(_mod, "_CREWAI_AVAILABLE", False)
    # Must not raise; no new handlers registered
    listener.setup_listeners(mock_bus)


# ---------------------------------------------------------------------------
# Auto-registration via the global bus
# ---------------------------------------------------------------------------

def test_crewai_bus_auto_registration(monkeypatch):
    """With a global bus present, __init__ calls setup_listeners automatically."""
    mock_bus = _make_mock_bus()
    monkeypatch.setattr(_mod, "_crewai_bus", mock_bus)

    CrewAIEventListener()

    assert "CrewKickoffStartedEvent" in mock_bus._handlers


# ---------------------------------------------------------------------------
# _register_optional_events
# ---------------------------------------------------------------------------

def test_register_optional_events_loop(mock_bus, monkeypatch):
    """_register_optional_events registers handlers for events that exist."""
    listener = _make_listener_with_mock_bus(mock_bus)

    import sys
    import types

    KnowledgeEvent = type("KnowledgeRetrievalStartedEvent", (), {})
    mock_crewai = types.ModuleType("crewai")
    mock_utilities = types.ModuleType("crewai.utilities")
    mock_events_pkg = types.ModuleType("crewai.utilities.events")
    mock_base_events = types.ModuleType("crewai.utilities.events.base_events")

    # Only set the event that "exists"
    mock_base_events.KnowledgeRetrievalStartedEvent = KnowledgeEvent
    # NonExistentEvent is intentionally NOT set; getattr returns None

    mock_crewai.utilities = mock_utilities
    mock_utilities.events = mock_events_pkg
    mock_events_pkg.base_events = mock_base_events

    monkeypatch.setitem(sys.modules, "crewai", mock_crewai)
    monkeypatch.setitem(sys.modules, "crewai.utilities", mock_utilities)
    monkeypatch.setitem(sys.modules, "crewai.utilities.events", mock_events_pkg)
    monkeypatch.setitem(sys.modules, "crewai.utilities.events.base_events", mock_base_events)

    mock_handler = MagicMock()
    listener._register_optional_events(mock_bus, [
        ("KnowledgeRetrievalStartedEvent", mock_handler),
        ("NonExistentEvent", mock_handler),  # skipped (getattr returns None)
    ])

    assert "KnowledgeRetrievalStartedEvent" in mock_bus._handlers
    assert "NonExistentEvent" not in mock_bus._handlers


# ---------------------------------------------------------------------------
# Failure handlers
# ---------------------------------------------------------------------------

def test_crew_kickoff_failed_captured(mock_bus):
    """CrewKickoffFailedEvent produces a crew_kickoff record with an error."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("CrewKickoffStartedEvent", crew_id="c1")
    fail = _make_crew_event("CrewKickoffFailedEvent", crew_id="c1", error="timeout")

    _fire_on_bus(mock_bus, "CrewKickoffStartedEvent", start)
    _fire_on_bus(mock_bus, "CrewKickoffFailedEvent", fail)

    records = listener.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "crew_kickoff"
    assert "error" in records[0]


def test_agent_error_captured(mock_bus):
    """AgentExecutionErrorEvent produces an agent_execution record with an error."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("AgentExecutionStartedEvent", agent_id="a1", role="Researcher")
    error = _make_crew_event("AgentExecutionErrorEvent", agent_id="a1", error="crashed")

    _fire_on_bus(mock_bus, "AgentExecutionStartedEvent", start)
    _fire_on_bus(mock_bus, "AgentExecutionErrorEvent", error)

    records = listener.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "agent_execution"
    assert "error" in records[0]


def test_fail_without_start_produces_error_record(mock_bus):
    """A fail event with no matching start produces a standalone error record."""
    listener = _make_listener_with_mock_bus(mock_bus, context_version="v9.0")

    fail = _make_crew_event("CrewKickoffFailedEvent", crew_id="orphan", error="oops")
    _fire_on_bus(mock_bus, "CrewKickoffFailedEvent", fail)

    records = listener.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "crew_kickoff"
    assert "error" in records[0]
    assert records[0].get("context_version") == "v9.0"
    assert "execution_time_ms" not in records[0]


# ---------------------------------------------------------------------------
# _safe_str exception path
# ---------------------------------------------------------------------------

def test_safe_str_exception_returns_unserializable():
    """_safe_str returns '<unserializable>' when str() raises."""
    from briefcase.integrations.frameworks.crewai_handler import _safe_str

    class Broken:
        def __str__(self):
            raise RuntimeError("str broken")

    result = _safe_str(Broken())
    assert result == "<unserializable>"


# ---------------------------------------------------------------------------
# Optional event handlers: knowledge
# ---------------------------------------------------------------------------

def test_knowledge_retrieval_captured(mock_bus):
    """Knowledge retrieval start + complete produce a knowledge_retrieval record."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("KnowledgeRetrievalStartedEvent", query_id="q1", query="What is RAG?")
    complete = _make_crew_event("KnowledgeRetrievalCompletedEvent", query_id="q1")

    listener._on_knowledge_started(start)
    listener._on_knowledge_completed(complete)

    records = listener.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "knowledge_retrieval"
    assert "execution_time_ms" in records[0]


def test_knowledge_search_failed_captured(mock_bus):
    """_on_knowledge_search_failed produces a standalone record."""
    listener = _make_listener_with_mock_bus(mock_bus)

    event = _make_crew_event("KnowledgeSearchQueryFailedEvent", query="test query")
    listener._on_knowledge_search_failed(event)

    records = listener.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "knowledge_search_failed"


def test_knowledge_query_start_complete(mock_bus):
    """Knowledge query start + complete pair up correctly."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("KnowledgeQueryStartedEvent", query_id="kq1", query="explain AI")
    complete = _make_crew_event("KnowledgeQueryCompletedEvent", query_id="kq1")

    listener._on_knowledge_query_started(start)
    listener._on_knowledge_query_completed(complete)

    records = listener.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "knowledge_query"


def test_knowledge_query_failed(mock_bus):
    """Knowledge query failure produces a knowledge_query record with an error."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("KnowledgeQueryStartedEvent", query_id="kq2", query="q")
    fail = _make_crew_event("KnowledgeQueryFailedEvent", query_id="kq2", error="timeout")

    listener._on_knowledge_query_started(start)
    listener._on_knowledge_query_failed(fail)

    records = listener.get_records()
    assert len(records) == 1
    assert "error" in records[0]


# ---------------------------------------------------------------------------
# Optional event handlers: memory
# ---------------------------------------------------------------------------

def test_memory_query_captured(mock_bus):
    """Memory query start + complete produce a memory_query record."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("MemoryQueryStartedEvent", query_id="m1", query="find docs")
    complete = _make_crew_event("MemoryQueryCompletedEvent", query_id="m1")

    listener._on_memory_query_started(start)
    listener._on_memory_query_completed(complete)

    records = listener.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "memory_query"


def test_memory_query_failed(mock_bus):
    """Memory query failure produces a memory_query record with an error."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("MemoryQueryStartedEvent", query_id="m2", query="q")
    fail = _make_crew_event("MemoryQueryFailedEvent", query_id="m2", error="not found")

    listener._on_memory_query_started(start)
    listener._on_memory_query_failed(fail)

    records = listener.get_records()
    assert len(records) == 1
    assert "error" in records[0]


def test_memory_save_captured(mock_bus):
    """Memory save start + complete produce a memory_save record."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("MemorySaveStartedEvent", item_id="s1", key="fact_42")
    complete = _make_crew_event("MemorySaveCompletedEvent", item_id="s1")

    listener._on_memory_save_started(start)
    listener._on_memory_save_completed(complete)

    records = listener.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "memory_save"


def test_memory_save_failed(mock_bus):
    """Memory save failure produces a memory_save record with an error."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("MemorySaveStartedEvent", item_id="s2", key="k")
    fail = _make_crew_event("MemorySaveFailedEvent", item_id="s2", error="disk full")

    listener._on_memory_save_started(start)
    listener._on_memory_save_failed(fail)

    records = listener.get_records()
    assert len(records) == 1
    assert "error" in records[0]


def test_memory_retrieval_captured(mock_bus):
    """Memory retrieval start + complete produce a memory_retrieval record."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("MemoryRetrievalStartedEvent", query_id="r1", query="recall")
    complete = _make_crew_event("MemoryRetrievalCompletedEvent", query_id="r1")

    listener._on_memory_retrieval_started(start)
    listener._on_memory_retrieval_completed(complete)

    records = listener.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "memory_retrieval"


# ---------------------------------------------------------------------------
# Optional event handlers: guardrails
# ---------------------------------------------------------------------------

def test_guardrail_captured(mock_bus):
    """Guardrail start + complete produce an llm_guardrail record."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("LLMGuardrailStartedEvent", guardrail_id="g1", name="safety_check")
    complete = _make_crew_event("LLMGuardrailCompletedEvent", guardrail_id="g1")

    listener._on_guardrail_started(start)
    listener._on_guardrail_completed(complete)

    records = listener.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "llm_guardrail"


# ---------------------------------------------------------------------------
# Optional event handlers: flow
# ---------------------------------------------------------------------------

def test_flow_events_captured(mock_bus):
    """Flow created + started + finished + plot all produce records."""
    listener = _make_listener_with_mock_bus(mock_bus)

    created = _make_crew_event("FlowCreatedEvent", flow_id="f1")
    listener._on_flow_created(created)

    start = _make_crew_event("FlowStartedEvent", flow_id="f1", name="DataFlow")
    finish = _make_crew_event("FlowFinishedEvent", flow_id="f1")
    listener._on_flow_started(start)
    listener._on_flow_finished(finish)

    plot = _make_crew_event("FlowPlotEvent", flow_id="f1")
    listener._on_flow_plot(plot)

    records = listener.get_records()
    assert len(records) == 3
    types = {r["decision_type"] for r in records}
    assert "flow_created" in types
    assert "flow" in types
    assert "flow_plot" in types


def test_method_execution_captured(mock_bus):
    """Method start + finish produce a method_execution record."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("MethodExecutionStartedEvent", method_id="me1", method_name="analyze")
    finish = _make_crew_event("MethodExecutionFinishedEvent", method_id="me1")

    listener._on_method_started(start)
    listener._on_method_finished(finish)

    records = listener.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "method_execution"


def test_method_execution_failed(mock_bus):
    """Method failure produces a method_execution record with an error."""
    listener = _make_listener_with_mock_bus(mock_bus)

    start = _make_crew_event("MethodExecutionStartedEvent", method_id="me2", method_name="analyze")
    fail = _make_crew_event("MethodExecutionFailedEvent", method_id="me2", error="crashed")

    listener._on_method_started(start)
    listener._on_method_failed(fail)

    records = listener.get_records()
    assert len(records) == 1
    assert "error" in records[0]


# ---------------------------------------------------------------------------
# Exception swallowing in event handlers
# ---------------------------------------------------------------------------

def test_crew_started_except_swallowed(mock_bus, monkeypatch):
    """An exception inside _on_crew_kickoff_started is swallowed."""
    listener = _make_listener_with_mock_bus(mock_bus)

    def raise_(*a, **k):
        raise RuntimeError("build failed")

    monkeypatch.setattr(listener, "_build_start_record", raise_)
    event = _make_crew_event("CrewKickoffStartedEvent", crew_id="c1")
    listener._on_crew_kickoff_started(event)  # must not raise


def test_crew_completed_except_swallowed(mock_bus, monkeypatch):
    """An exception inside _on_crew_kickoff_completed is swallowed."""
    listener = _make_listener_with_mock_bus(mock_bus)

    def raise_(*a, **k):
        raise RuntimeError("complete failed")

    monkeypatch.setattr(listener, "_complete", raise_)
    event = _make_crew_event("CrewKickoffCompletedEvent", crew_id="c1")
    listener._on_crew_kickoff_completed(event)  # must not raise


def test_knowledge_search_failed_except_swallowed(mock_bus, monkeypatch):
    """An exception inside _on_knowledge_search_failed is swallowed."""
    listener = _make_listener_with_mock_bus(mock_bus)

    def raise_(*a, **k):
        raise RuntimeError("build failed")

    monkeypatch.setattr(listener, "_build_event_record", raise_)
    event = _make_crew_event("KnowledgeSearchQueryFailedEvent", query="test")
    listener._on_knowledge_search_failed(event)  # must not raise


def test_flow_created_except_swallowed(mock_bus, monkeypatch):
    """An exception inside _on_flow_created is swallowed."""
    listener = _make_listener_with_mock_bus(mock_bus)

    def raise_(*a, **k):
        raise RuntimeError("build failed")

    monkeypatch.setattr(listener, "_build_event_record", raise_)
    event = _make_crew_event("FlowCreatedEvent", flow_id="f1")
    listener._on_flow_created(event)  # must not raise


def test_flow_plot_except_swallowed(mock_bus, monkeypatch):
    """An exception inside _on_flow_plot is swallowed."""
    listener = _make_listener_with_mock_bus(mock_bus)

    def raise_(*a, **k):
        raise RuntimeError("build failed")

    monkeypatch.setattr(listener, "_build_event_record", raise_)
    event = _make_crew_event("FlowPlotEvent", flow_id="f1")
    listener._on_flow_plot(event)  # must not raise


def test_generic_start_except_swallowed(mock_bus, monkeypatch):
    """An exception inside _generic_start is swallowed."""
    listener = _make_listener_with_mock_bus(mock_bus)

    def raise_(*a, **k):
        raise RuntimeError("extract failed")

    monkeypatch.setattr(
        "briefcase.integrations.frameworks.crewai_handler._extract_event_key",
        raise_,
    )
    event = _make_crew_event("KnowledgeRetrievalStartedEvent", query_id="q1")
    listener._generic_start("knowledge", "knowledge_retrieval", event, "query_id")


def test_generic_complete_except_swallowed(mock_bus, monkeypatch):
    """An exception inside _generic_complete is swallowed."""
    listener = _make_listener_with_mock_bus(mock_bus)

    def raise_(*a, **k):
        raise RuntimeError("complete failed")

    monkeypatch.setattr(listener, "_complete", raise_)
    event = _make_crew_event("KnowledgeRetrievalCompletedEvent", query_id="q1")
    listener._generic_complete("knowledge", "knowledge_retrieval", event)


def test_generic_fail_except_swallowed(mock_bus, monkeypatch):
    """An exception inside _generic_fail is swallowed."""
    listener = _make_listener_with_mock_bus(mock_bus)

    def raise_(*a, **k):
        raise RuntimeError("fail failed")

    monkeypatch.setattr(listener, "_fail", raise_)
    event = _make_crew_event("KnowledgeQueryFailedEvent", query_id="q1")
    listener._generic_fail("knowledge_query", "knowledge_query", event)


def test_register_optional_events_bus_on_exception_swallowed(mock_bus, monkeypatch):
    """An exception from bus.on() inside _register_optional_events is swallowed."""
    import sys
    import types

    KnowledgeEvent = type("KnowledgeRetrievalStartedEvent", (), {})
    mock_base_events = types.ModuleType("crewai.utilities.events.base_events")
    mock_base_events.KnowledgeRetrievalStartedEvent = KnowledgeEvent

    monkeypatch.setitem(sys.modules, "crewai", types.ModuleType("crewai"))
    monkeypatch.setitem(sys.modules, "crewai.utilities", types.ModuleType("crewai.utilities"))
    monkeypatch.setitem(sys.modules, "crewai.utilities.events", types.ModuleType("crewai.utilities.events"))
    monkeypatch.setitem(sys.modules, "crewai.utilities.events.base_events", mock_base_events)

    listener = _make_listener_with_mock_bus(mock_bus)

    def bad_on(event_cls):
        raise RuntimeError("bus.on failed")

    mock_bus.on = bad_on

    # Must not raise even when bus.on() fails
    listener._register_optional_events(mock_bus, [
        ("KnowledgeRetrievalStartedEvent", MagicMock()),
    ])
