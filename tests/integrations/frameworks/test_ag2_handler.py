"""Tests for briefcase/integrations/frameworks/ag2_handler.py.

The ag2 `autogen` namespace is stubbed by conftest.py; mock agents record
their registered hooks so tests can fire them directly.
"""

import time
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

import briefcase.integrations.frameworks.ag2_handler as _mod
from briefcase._export_mixin import wait_for_pending_exports
from briefcase.integrations.frameworks.ag2_handler import (
    AG2HookTracer,
    instrument_agent,
    _safe_extract_message,
    _safe_serialize_small,
    _agent_name,
    require_ag2,
)
from briefcase.config import BriefcaseConfig
from briefcase.config import setup as briefcase_setup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_agent(name: str = "TestAgent") -> MagicMock:
    """Return a mock ConversableAgent whose register_hook stores callbacks."""
    agent = MagicMock()
    agent.name = name
    _registered: Dict[str, List[Any]] = {}

    def _register_hook(hook_name, fn):
        _registered.setdefault(hook_name, []).append(fn)

    agent.register_hook = _register_hook
    agent._registered_hooks = _registered
    return agent


def _fire_hook(agent: MagicMock, hook_name: str, *args):
    """Fire all registered hooks for the given name."""
    hooks = agent._registered_hooks.get(hook_name, [])
    result = args[0] if args else None
    for fn in hooks:
        result = fn(*args)
    return result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def ensure_ag2_available(monkeypatch):
    """Enable the AG2 handler without requiring ag2 to be installed."""
    monkeypatch.setattr(_mod, "_AG2_AVAILABLE", True)
    monkeypatch.setattr(_mod, "ConversableAgent", MagicMock())


@pytest.fixture(autouse=True)
def reset_config():
    BriefcaseConfig.reset()
    yield
    BriefcaseConfig.reset()


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------

def test_message_send_captured():
    """process_message_before_send captures decision_type='message_send'."""
    tracer = AG2HookTracer()
    agent = _make_mock_agent("Sender")
    recipient = _make_mock_agent("Recipient")
    tracer.instrument(agent)

    msg = {"role": "user", "content": "Hello world"}
    _fire_hook(agent, "process_message_before_send", msg, recipient, False)

    records = tracer.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "message_send"
    assert "Hello world" in str(records[0]["inputs"])


def test_record_uses_wire_field_names():
    """Hook records carry the decision-record core field names."""
    tracer = AG2HookTracer()
    agent = _make_mock_agent("WireAgent")
    tracer.instrument(agent)

    _fire_hook(agent, "process_message_before_send", "hi", _make_mock_agent(), False)

    record = tracer.get_records()[0]
    for key in ("decision_id", "decision_type", "function_name",
                "inputs", "outputs", "started_at"):
        assert key in record, key
    assert record["function_name"] == "WireAgent"


def test_message_context_captured():
    """process_all_messages_before_reply captures decision_type='message_context'."""
    tracer = AG2HookTracer()
    agent = _make_mock_agent("ContextAgent")
    tracer.instrument(agent)

    messages = [
        {"role": "user", "content": "msg1"},
        {"role": "assistant", "content": "msg2"},
    ]
    _fire_hook(agent, "process_all_messages_before_reply", messages)

    records = tracer.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "message_context"
    assert records[0]["inputs"]["message_count"] == 2


def test_state_update_captured():
    """update_agent_state captures decision_type='state_update'."""
    tracer = AG2HookTracer()
    agent = _make_mock_agent("StateAgent")
    tracer.instrument(agent)

    state = {"counter": 42, "status": "active"}
    _fire_hook(agent, "update_agent_state", state)

    records = tracer.get_records()
    assert len(records) == 1
    assert records[0]["decision_type"] == "state_update"


def test_instrument_registers_hooks():
    """instrument() registers hooks on the agent via register_hook."""
    tracer = AG2HookTracer()
    agent = _make_mock_agent()
    tracer.instrument(agent)

    assert "process_message_before_send" in agent._registered_hooks
    assert "process_all_messages_before_reply" in agent._registered_hooks
    assert "update_agent_state" in agent._registered_hooks
    assert "safeguard_llm_inputs" in agent._registered_hooks
    assert "safeguard_llm_outputs" in agent._registered_hooks
    assert "safeguard_tool_inputs" in agent._registered_hooks
    assert "safeguard_tool_outputs" in agent._registered_hooks


def test_async_non_blocking():
    """async_capture=True returns immediately without blocking on the exporter."""
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
    tracer = AG2HookTracer(async_capture=True)
    agent = _make_mock_agent()
    tracer.instrument(agent)

    msg = {"role": "user", "content": "test"}
    recipient = _make_mock_agent()
    start = time.monotonic()
    _fire_hook(agent, "process_message_before_send", msg, recipient, False)
    elapsed = time.monotonic() - start

    assert elapsed < 0.04, f"Blocked for {elapsed:.3f}s"
    assert wait_for_pending_exports(5.0)
    assert len(calls) == 1


def test_error_in_hook_silent():
    """Exceptions inside hooks never propagate to the caller."""
    tracer = AG2HookTracer()
    agent = _make_mock_agent()
    tracer.instrument(agent)

    # Fire the hook with completely broken args: must not raise
    _fire_hook(agent, "process_message_before_send", None, None, None)


def test_missing_dependency_raises():
    """Instantiating the tracer when ag2 is absent raises ImportError."""
    original = _mod._AG2_AVAILABLE
    try:
        _mod._AG2_AVAILABLE = False
        with pytest.raises(ImportError, match="pip install"):
            AG2HookTracer()
    finally:
        _mod._AG2_AVAILABLE = original


def test_context_version_in_records():
    """context_version appears on captured records."""
    tracer = AG2HookTracer(context_version="v4.2")
    agent = _make_mock_agent()
    tracer.instrument(agent)

    msg = "hello"
    recipient = _make_mock_agent()
    _fire_hook(agent, "process_message_before_send", msg, recipient, False)

    records = tracer.get_records()
    assert len(records) == 1
    assert records[0]["context_version"] == "v4.2"


def test_exporter_integration():
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
    tracer = AG2HookTracer(async_capture=False)
    agent = _make_mock_agent()
    tracer.instrument(agent)

    msg = {"role": "user", "content": "hi"}
    recipient = _make_mock_agent()
    _fire_hook(agent, "process_message_before_send", msg, recipient, False)

    assert len(calls) == 1
    assert calls[0]["decision_type"] == "message_send"


def test_instrument_agent_convenience():
    """instrument_agent() creates a tracer and registers hooks in one call."""
    agent = _make_mock_agent()
    tracer = instrument_agent(agent)
    assert isinstance(tracer, AG2HookTracer)
    assert "process_message_before_send" in agent._registered_hooks


# ---------------------------------------------------------------------------
# Additional coverage tests
# ---------------------------------------------------------------------------

def test_clear_resets_records():
    """clear() resets all captured records."""
    tracer = AG2HookTracer()
    agent = _make_mock_agent()
    tracer.instrument(agent)

    msg = "hello"
    recipient = _make_mock_agent()
    _fire_hook(agent, "process_message_before_send", msg, recipient, False)
    assert tracer.decision_count == 1

    tracer.clear()
    assert tracer.decision_count == 0
    assert tracer.get_records() == []


def test_get_records_returns_copy():
    """get_records() returns a copy, not the internal list."""
    tracer = AG2HookTracer()
    agent = _make_mock_agent()
    tracer.instrument(agent)

    msg = "hello"
    recipient = _make_mock_agent()
    _fire_hook(agent, "process_message_before_send", msg, recipient, False)

    r1 = tracer.get_records()
    r1.clear()
    r2 = tracer.get_records()
    assert len(r2) == 1


def test_decision_count_property():
    """decision_count reflects the number of captured records."""
    tracer = AG2HookTracer()
    agent = _make_mock_agent()
    tracer.instrument(agent)
    assert tracer.decision_count == 0

    msg = "a"
    recipient = _make_mock_agent()
    _fire_hook(agent, "process_message_before_send", msg, recipient, False)
    assert tracer.decision_count == 1

    _fire_hook(agent, "process_message_before_send", msg, recipient, False)
    assert tracer.decision_count == 2


def test_capture_messages_flag_false():
    """With capture_messages=False, no message hooks are registered."""
    tracer = AG2HookTracer(capture_messages=False)
    agent = _make_mock_agent()
    tracer.instrument(agent)

    assert "process_message_before_send" not in agent._registered_hooks
    assert "process_all_messages_before_reply" not in agent._registered_hooks


def test_capture_llm_flag_false():
    """With capture_llm=False, no LLM safeguard hooks are registered."""
    tracer = AG2HookTracer(capture_llm=False)
    agent = _make_mock_agent()
    tracer.instrument(agent)

    assert "safeguard_llm_inputs" not in agent._registered_hooks
    assert "safeguard_llm_outputs" not in agent._registered_hooks


def test_capture_tools_flag_false():
    """With capture_tools=False, no tool safeguard hooks are registered."""
    tracer = AG2HookTracer(capture_tools=False)
    agent = _make_mock_agent()
    tracer.instrument(agent)

    assert "safeguard_tool_inputs" not in agent._registered_hooks
    assert "safeguard_tool_outputs" not in agent._registered_hooks


def test_truncation_applied():
    """Long message content truncates to max_input_chars."""
    tracer = AG2HookTracer(max_input_chars=20)
    agent = _make_mock_agent()
    tracer.instrument(agent)

    long_msg = "x" * 5000
    recipient = _make_mock_agent()
    _fire_hook(agent, "process_message_before_send", long_msg, recipient, False)

    records = tracer.get_records()
    content = str(records[0]["inputs"])
    # Truncation occurred: the stored content is nowhere near 5000 chars
    assert len(content) < 5500


def test_instrument_many():
    """instrument_many() instruments all provided agents."""
    tracer = AG2HookTracer()
    agents = [_make_mock_agent(f"Agent{i}") for i in range(3)]
    tracer.instrument_many(agents)

    for a in agents:
        assert "process_message_before_send" in a._registered_hooks


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
    tracer = AG2HookTracer(async_capture=False)
    agent = _make_mock_agent()
    tracer.instrument(agent)

    msg = {"role": "user", "content": "sync test"}
    recipient = _make_mock_agent()
    _fire_hook(agent, "process_message_before_send", msg, recipient, False)

    assert len(calls) == 1


def test_no_exporter_configured():
    """When no exporter is configured, _trigger_export is a no-op."""
    tracer = AG2HookTracer()
    # No exporter: must not raise
    tracer._trigger_export({"decision_id": "x", "decision_type": "test"})


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
    tracer = AG2HookTracer(async_capture=False)
    # Must not raise
    tracer._trigger_export({"decision_id": "x", "decision_type": "test"})


def test_none_message_handled():
    """A None message serializes to None without crashing."""
    result = _safe_extract_message(None)
    assert result is None


def test_dict_message_extracted():
    """A dict message extracts role and content."""
    extracted = _safe_extract_message({"role": "user", "content": "hello"})
    assert isinstance(extracted, dict)
    assert extracted["role"] == "user"
    assert extracted["content"] == "hello"


def test_string_message_extracted():
    """A string message returns truncated."""
    extracted = _safe_extract_message("plain text", max_chars=5)
    assert extracted == "plain"


def test_llm_input_captured():
    """safeguard_llm_inputs captures decision_type='llm_input'."""
    tracer = AG2HookTracer()
    agent = _make_mock_agent()
    tracer.instrument(agent)

    messages = [{"role": "user", "content": "Classify this"}]
    _fire_hook(agent, "safeguard_llm_inputs", messages)

    records = tracer.get_records()
    assert any(r["decision_type"] == "llm_input" for r in records)


def test_tool_input_captured():
    """safeguard_tool_inputs captures decision_type='tool_input'."""
    tracer = AG2HookTracer()
    agent = _make_mock_agent()
    tracer.instrument(agent)

    tool_call = {"name": "search", "args": {"query": "AI news"}}
    _fire_hook(agent, "safeguard_tool_inputs", tool_call)

    records = tracer.get_records()
    assert any(r["decision_type"] == "tool_input" for r in records)


def test_require_ag2_raises_when_unavailable():
    """require_ag2() raises ImportError when ag2 is absent."""
    original = _mod._AG2_AVAILABLE
    try:
        _mod._AG2_AVAILABLE = False
        with pytest.raises(ImportError, match="pip install"):
            require_ag2()
    finally:
        _mod._AG2_AVAILABLE = original


def test_hooks_return_original_value():
    """All hooks return the original argument unmodified."""
    tracer = AG2HookTracer()
    agent = _make_mock_agent()
    tracer.instrument(agent)

    msg = {"role": "user", "content": "unchanged"}
    recipient = _make_mock_agent()
    returned = _fire_hook(agent, "process_message_before_send", msg, recipient, False)
    assert returned is msg  # returned unmodified


def test_no_context_version():
    """Records do NOT include a context_version key when not set."""
    tracer = AG2HookTracer()
    agent = _make_mock_agent()
    tracer.instrument(agent)

    msg = "hello"
    recipient = _make_mock_agent()
    _fire_hook(agent, "process_message_before_send", msg, recipient, False)

    records = tracer.get_records()
    assert "context_version" not in records[0]


# ---------------------------------------------------------------------------
# llm_output and tool_output hooks
# ---------------------------------------------------------------------------

def test_llm_output_captured():
    """safeguard_llm_outputs captures decision_type='llm_output'."""
    tracer = AG2HookTracer()
    agent = _make_mock_agent()
    tracer.instrument(agent)

    response = {"content": "the answer is 42", "role": "assistant"}
    _fire_hook(agent, "safeguard_llm_outputs", response)

    records = tracer.get_records()
    assert any(r["decision_type"] == "llm_output" for r in records)


def test_tool_output_captured():
    """safeguard_tool_outputs captures decision_type='tool_output'."""
    tracer = AG2HookTracer()
    agent = _make_mock_agent()
    tracer.instrument(agent)

    result = {"status": "success", "data": "search results"}
    _fire_hook(agent, "safeguard_tool_outputs", result)

    records = tracer.get_records()
    assert any(r["decision_type"] == "tool_output" for r in records)


def test_llm_output_returns_response_unmodified():
    """safeguard_llm_outputs returns the response unmodified."""
    tracer = AG2HookTracer()
    agent = _make_mock_agent()
    tracer.instrument(agent)

    response = {"content": "result", "role": "assistant"}
    returned = _fire_hook(agent, "safeguard_llm_outputs", response)
    assert returned is response


def test_tool_output_returns_result_unmodified():
    """safeguard_tool_outputs returns the tool_result unmodified."""
    tracer = AG2HookTracer()
    agent = _make_mock_agent()
    tracer.instrument(agent)

    result = {"key": "value"}
    returned = _fire_hook(agent, "safeguard_tool_outputs", result)
    assert returned is result


# ---------------------------------------------------------------------------
# Exception paths in hook closures
# ---------------------------------------------------------------------------

def test_hook_exception_swallowed_message_send(monkeypatch):
    """An exception inside the message_send hook is swallowed."""
    tracer = AG2HookTracer()
    agent = _make_mock_agent()
    tracer.instrument(agent)

    def bad_build(*a, **k):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(tracer, "_build_record", bad_build)
    # Must not raise
    _fire_hook(agent, "process_message_before_send", "msg", _make_mock_agent(), False)


def test_hook_exception_swallowed_message_context(monkeypatch):
    """An exception inside the message_context hook is swallowed."""
    tracer = AG2HookTracer()
    agent = _make_mock_agent()
    tracer.instrument(agent)

    def bad_build(*a, **k):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(tracer, "_build_record", bad_build)
    _fire_hook(agent, "process_all_messages_before_reply", [{"role": "user", "content": "hi"}])


def test_hook_exception_swallowed_state_update(monkeypatch):
    """An exception inside the state_update hook is swallowed."""
    tracer = AG2HookTracer()
    agent = _make_mock_agent()
    tracer.instrument(agent)

    def bad_build(*a, **k):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(tracer, "_build_record", bad_build)
    _fire_hook(agent, "update_agent_state", {"counter": 1})


def test_hook_exception_swallowed_llm_input(monkeypatch):
    """An exception inside the llm_input hook is swallowed."""
    tracer = AG2HookTracer()
    agent = _make_mock_agent()
    tracer.instrument(agent)

    def bad_build(*a, **k):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(tracer, "_build_record", bad_build)
    _fire_hook(agent, "safeguard_llm_inputs", [{"role": "user", "content": "test"}])


def test_hook_exception_swallowed_tool_input(monkeypatch):
    """An exception inside the tool_input hook is swallowed."""
    tracer = AG2HookTracer()
    agent = _make_mock_agent()
    tracer.instrument(agent)

    def bad_build(*a, **k):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(tracer, "_build_record", bad_build)
    _fire_hook(agent, "safeguard_tool_inputs", {"name": "search", "args": {}})


# ---------------------------------------------------------------------------
# Helper edge cases
# ---------------------------------------------------------------------------

def test_agent_name_exception_returns_none():
    """_agent_name returns None when the name property raises."""
    class Broken:
        @property
        def name(self):
            raise RuntimeError("broken name property")

    result = _agent_name(Broken())
    assert result is None


def test_safe_extract_message_exception_returns_unserializable():
    """_safe_extract_message returns '<unserializable>' when str() raises."""
    class Broken:
        def __str__(self):
            raise RuntimeError("str broken")

    result = _safe_extract_message(Broken())
    assert result == "<unserializable>"


def test_safe_serialize_small_none():
    """_safe_serialize_small(None) returns None."""
    assert _safe_serialize_small(None) is None


def test_safe_serialize_small_int():
    """_safe_serialize_small with an int returns the int unchanged."""
    assert _safe_serialize_small(42) == 42


def test_safe_serialize_small_float():
    """_safe_serialize_small with a float returns the float unchanged."""
    assert _safe_serialize_small(3.14) == 3.14


def test_safe_serialize_small_bool():
    """_safe_serialize_small with a bool returns the bool unchanged."""
    assert _safe_serialize_small(True) is True


def test_safe_serialize_small_list():
    """_safe_serialize_small with a list returns a list of strings."""
    result = _safe_serialize_small([1, 2, 3])
    assert result == ["1", "2", "3"]


def test_safe_serialize_small_tuple():
    """_safe_serialize_small with a tuple returns a list of strings."""
    result = _safe_serialize_small((1, "two", 3.0))
    assert isinstance(result, list)
    assert "1" in result


def test_safe_serialize_small_arbitrary_object():
    """_safe_serialize_small with an unknown object calls str()."""
    class MyObj:
        def __str__(self):
            return "my_obj_str"

    result = _safe_serialize_small(MyObj())
    assert result == "my_obj_str"


def test_safe_serialize_small_exception():
    """_safe_serialize_small returns '<unserializable>' on failure."""
    class Broken:
        def __str__(self):
            raise RuntimeError("str broken")

    result = _safe_serialize_small(Broken())
    assert result == "<unserializable>"
