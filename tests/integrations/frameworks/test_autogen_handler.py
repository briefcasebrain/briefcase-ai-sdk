"""Tests for briefcase/integrations/frameworks/autogen_handler.py.

autogen_agentchat is stubbed by conftest.py; events are delivered as
logging.LogRecord objects carrying duck-typed message fakes.
"""

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

import briefcase.integrations.frameworks.autogen_handler as _mod
from briefcase._export_mixin import wait_for_pending_exports
from briefcase.integrations.frameworks.autogen_handler import (
    AutoGenEventHandler,
    install,
    uninstall,
    EVENT_LOGGER_NAME,
    TRACE_LOGGER_NAME,
)
from briefcase.config import BriefcaseConfig
from briefcase.config import setup as briefcase_setup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_log_record(msg: Any, logger_name: str = None) -> logging.LogRecord:
    """Return a logging.LogRecord with the given msg."""
    if logger_name is None:
        logger_name = EVENT_LOGGER_NAME
    record = logging.LogRecord(
        name=logger_name,
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg=msg,
        args=(),
        exc_info=None,
    )
    return record


def _make_text_message(source: str = "agent1", content: str = "Hello") -> Any:
    """Return a mock TextMessage-like object."""
    msg = MagicMock()
    msg.source = source
    msg.content = content
    del msg.tool_calls  # ensure no tool_calls attr
    del msg.results
    type(msg).__name__ = "TextMessage"
    return msg


def _make_tool_call_request(source: str = "agent1") -> Any:
    """Return a mock ToolCallRequestEvent-like object."""
    event = MagicMock()
    event.source = source
    event.tool_calls = [{"name": "search", "args": {"query": "test"}}]
    del event.results
    del event.content
    type(event).__name__ = "ToolCallRequestEvent"
    return event


def _make_tool_call_execution(source: str = "agent1") -> Any:
    """Return a mock ToolCallExecutionEvent-like object."""
    event = MagicMock()
    event.source = source
    event.results = [{"call_id": "c1", "content": "result text"}]
    del event.tool_calls
    del event.content
    type(event).__name__ = "ToolCallExecutionEvent"
    return event


def _make_tool_call_summary(source: str = "agent1", content: str = "summary") -> Any:
    """Return a mock ToolCallSummaryMessage-like object."""
    msg = MagicMock()
    msg.source = source
    msg.content = content
    del msg.tool_calls
    del msg.results
    type(msg).__name__ = "ToolCallSummaryMessage"
    return msg


def _make_stop_message(source: str = "agent1", content: str = "done") -> Any:
    """Return a mock StopMessage-like object."""
    msg = MagicMock()
    msg.source = source
    msg.content = content
    del msg.tool_calls
    del msg.results
    type(msg).__name__ = "StopMessage"
    return msg


def _make_handoff_message(source: str = "agent1", target: str = "agent2") -> Any:
    """Return a mock HandoffMessage-like object."""
    msg = MagicMock()
    msg.source = source
    msg.target = target
    msg.content = "handoff"
    del msg.tool_calls
    del msg.results
    type(msg).__name__ = "HandoffMessage"
    return msg


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def ensure_autogen_available(monkeypatch):
    """Enable the AutoGen handler without requiring autogen_agentchat."""
    monkeypatch.setattr(_mod, "_AUTOGEN_AVAILABLE", True)


@pytest.fixture(autouse=True)
def reset_state():
    _mod._INSTALLED_HANDLER = None
    BriefcaseConfig.reset()
    yield
    _mod._INSTALLED_HANDLER = None
    BriefcaseConfig.reset()


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------

def test_text_message_captured():
    """TextMessage events are captured as decision_type='text_message'."""
    handler = AutoGenEventHandler()
    handler.attach()

    msg = _make_text_message(source="Alice", content="Hello world")
    record = _make_log_record(msg)
    handler.emit(record)

    records = handler.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "text_message"
    assert records[0]["inputs"]["source"] == "Alice"
    assert "Hello world" in records[0]["inputs"]["content"]
    assert records[0]["function_name"] == "Alice"

    handler.detach()


def test_tool_call_request_captured():
    """ToolCallRequestEvent is captured as decision_type='tool_call_request'."""
    handler = AutoGenEventHandler()
    handler.attach()

    event = _make_tool_call_request(source="ToolAgent")
    record = _make_log_record(event)
    handler.emit(record)

    records = handler.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "tool_call_request"

    handler.detach()


def test_tool_call_execution_captured():
    """ToolCallExecutionEvent is captured as decision_type='tool_call_execution'."""
    handler = AutoGenEventHandler()
    handler.attach()

    event = _make_tool_call_execution(source="Executor")
    record = _make_log_record(event)
    handler.emit(record)

    records = handler.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "tool_call_execution"

    handler.detach()


def test_tool_call_summary_captured():
    """ToolCallSummaryMessage is captured as decision_type='tool_call_summary'."""
    handler = AutoGenEventHandler()
    handler.attach()

    msg = _make_tool_call_summary(source="Summarizer", content="All done")
    record = _make_log_record(msg)
    handler.emit(record)

    records = handler.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "tool_call_summary"

    handler.detach()


def test_async_non_blocking():
    """async_capture=True returns without blocking on the exporter."""
    calls = []

    class SlowExporter:
        async def export(self, record):
            import time
            time.sleep(0.05)
            calls.append(record)

        async def flush(self):
            pass

        async def close(self):
            pass

    briefcase_setup(exporter=SlowExporter())
    handler = AutoGenEventHandler(async_capture=True)
    handler.attach()

    import time
    msg = _make_text_message()
    log_record = _make_log_record(msg)
    start = time.monotonic()
    handler.emit(log_record)
    elapsed = time.monotonic() - start

    assert elapsed < 0.04, f"Blocked for {elapsed:.3f}s"
    assert wait_for_pending_exports(5.0)
    assert len(calls) == 1

    handler.detach()


def test_error_in_emit_silent():
    """Exceptions during emit never propagate to the caller."""
    handler = AutoGenEventHandler()

    bad_record = logging.LogRecord(
        name=EVENT_LOGGER_NAME, level=logging.INFO,
        pathname="", lineno=0, msg=object(), args=(), exc_info=None,
    )
    handler.emit(bad_record)  # must not raise


def test_missing_dependency_raises():
    """Instantiating the handler when autogen-agentchat is absent raises."""
    original = _mod._AUTOGEN_AVAILABLE
    try:
        _mod._AUTOGEN_AVAILABLE = False
        with pytest.raises(ImportError, match="pip install"):
            AutoGenEventHandler()
    finally:
        _mod._AUTOGEN_AVAILABLE = original


def test_context_version_in_records():
    """context_version appears on captured records."""
    handler = AutoGenEventHandler(context_version="v9.1")
    handler.attach()

    msg = _make_text_message()
    record = _make_log_record(msg)
    handler.emit(record)

    records = handler.get_records()
    assert len(records) == 1
    assert records[0]["context_version"] == "v9.1"

    handler.detach()


def test_install_attaches_handler():
    """install() attaches the handler to AutoGen's event loggers."""
    handler = install()
    assert isinstance(handler, AutoGenEventHandler)
    assert _mod._INSTALLED_HANDLER is handler

    event_lg = logging.getLogger(EVENT_LOGGER_NAME)
    assert handler in event_lg.handlers

    handler.detach()


def test_install_idempotent():
    """Calling install() twice returns the same instance."""
    h1 = install()
    h2 = install()
    assert h1 is h2

    h1.detach()


def test_uninstall_detaches_handler():
    """uninstall() detaches the installed handler and forgets it."""
    handler = install()
    event_lg = logging.getLogger(EVENT_LOGGER_NAME)
    assert handler in event_lg.handlers

    uninstall()
    assert handler not in event_lg.handlers
    assert _mod._INSTALLED_HANDLER is None


def test_uninstall_without_install_is_noop():
    """uninstall() with nothing installed does not raise."""
    uninstall()
    assert _mod._INSTALLED_HANDLER is None


# ---------------------------------------------------------------------------
# Additional coverage tests
# ---------------------------------------------------------------------------

def test_clear_resets_records():
    """clear() resets all captured records."""
    handler = AutoGenEventHandler()
    handler.attach()

    msg = _make_text_message()
    record = _make_log_record(msg)
    handler.emit(record)
    assert handler.decision_count == 1

    handler.clear()
    assert handler.decision_count == 0

    handler.detach()


def test_get_records_returns_copy():
    """get_records() returns a copy."""
    handler = AutoGenEventHandler()
    handler.attach()

    msg = _make_text_message()
    record = _make_log_record(msg)
    handler.emit(record)

    r1 = handler.get_records()
    r1.clear()
    r2 = handler.get_records()
    assert len(r2) == 1

    handler.detach()


def test_decision_count_property():
    """decision_count reflects the captured record count."""
    handler = AutoGenEventHandler()
    handler.attach()
    assert handler.decision_count == 0

    msg = _make_text_message()
    record = _make_log_record(msg)
    handler.emit(record)
    assert handler.decision_count == 1

    handler.detach()


def test_attach_and_detach():
    """attach() and detach() add and remove the handler from loggers."""
    handler = AutoGenEventHandler()
    handler.attach()

    event_lg = logging.getLogger(EVENT_LOGGER_NAME)
    assert handler in event_lg.handlers

    handler.detach()
    assert handler not in event_lg.handlers


def test_unknown_event_captured_as_autogen_event():
    """Unknown event objects are captured as decision_type='autogen_event'."""
    handler = AutoGenEventHandler()

    class UnknownEvent:
        pass

    record = _make_log_record(UnknownEvent())
    handler.emit(record)

    records = handler.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "autogen_event"


def test_malformed_record_silent():
    """A LogRecord with a None msg does not raise."""
    handler = AutoGenEventHandler()
    record = _make_log_record(None)
    handler.emit(record)  # must not raise


def test_sync_export():
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
    handler = AutoGenEventHandler(async_capture=False)
    handler.attach()

    msg = _make_text_message()
    record = _make_log_record(msg)
    handler.emit(record)

    assert len(calls) == 1

    handler.detach()


def test_no_exporter_configured():
    """When no exporter is configured, _trigger_export is a no-op."""
    handler = AutoGenEventHandler()
    # No exporter: must not raise
    handler._trigger_export({"decision_id": "x", "decision_type": "test"})


def test_export_failure_silent():
    """Exporter errors never propagate."""

    class BrokenExporter:
        async def export(self, record):
            raise RuntimeError("export failed")

        async def flush(self):
            pass

        async def close(self):
            pass

    briefcase_setup(exporter=BrokenExporter())
    handler = AutoGenEventHandler(async_capture=False)
    handler._trigger_export({"decision_id": "x", "decision_type": "test"})


def test_handoff_message_captured():
    """HandoffMessage is captured as a control_message with subtype='handoff'."""
    handler = AutoGenEventHandler()
    msg = _make_handoff_message(source="Alice", target="Bob")
    record = _make_log_record(msg)
    handler.emit(record)

    records = handler.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "control_message"
    assert records[0]["inputs"].get("subtype") == "handoff"


def test_stop_message_captured():
    """StopMessage is captured as a control_message with subtype='stop'."""
    handler = AutoGenEventHandler()
    msg = _make_stop_message()
    record = _make_log_record(msg)
    handler.emit(record)

    records = handler.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "control_message"
    assert records[0]["inputs"].get("subtype") == "stop"


def test_traces_flag_false_skips_trace_records():
    """With capture_traces=False, TRACE_LOGGER_NAME records are skipped."""
    handler = AutoGenEventHandler(capture_traces=False)
    handler.attach()

    msg = _make_text_message()
    trace_record = _make_log_record(msg, logger_name=TRACE_LOGGER_NAME)
    handler.emit(trace_record)

    assert handler.decision_count == 0

    handler.detach()


def test_events_flag_false_skips_event_records():
    """With capture_events=False, EVENT_LOGGER_NAME records are skipped."""
    handler = AutoGenEventHandler(capture_events=False)
    handler.attach()

    msg = _make_text_message()
    event_record = _make_log_record(msg, logger_name=EVENT_LOGGER_NAME)
    handler.emit(event_record)

    assert handler.decision_count == 0

    handler.detach()


def test_install_missing_dependency():
    """install() raises ImportError when autogen-agentchat is absent."""
    original = _mod._AUTOGEN_AVAILABLE
    try:
        _mod._AUTOGEN_AVAILABLE = False
        with pytest.raises(ImportError, match="pip install"):
            install()
    finally:
        _mod._AUTOGEN_AVAILABLE = original


def test_no_context_version():
    """Records do NOT include a context_version key when not set."""
    handler = AutoGenEventHandler()
    msg = _make_text_message()
    record = _make_log_record(msg)
    handler.emit(record)

    records = handler.get_records()
    assert "context_version" not in records[0]


def test_string_msg_captured():
    """Plain string log messages are captured as autogen_event."""
    handler = AutoGenEventHandler()
    record = _make_log_record("plain string message")
    handler.emit(record)

    records = handler.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "autogen_event"
    assert records[0]["function_name"] == "autogen"
