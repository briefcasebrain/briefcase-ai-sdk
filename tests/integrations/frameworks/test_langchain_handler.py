"""Tests for briefcase/integrations/frameworks/langchain_handler.py.

Covers the callback lifecycle contract plus helpers and edge cases. The
langchain_core package is stubbed by conftest.py; the handler is duck-typed,
so the tests drive it directly with fake payloads.
"""

import asyncio
import builtins
import time
import uuid
from datetime import timezone
from unittest.mock import MagicMock, patch

import pytest

from briefcase._export_mixin import wait_for_pending_exports
from briefcase.integrations.frameworks.langchain_handler import (
    BriefcaseLangChainHandler,
    CapturedDecision,
    require_langchain,
    _extract_model_name,
    _extract_model_params,
    _extract_chain_name,
    _extract_llm_output,
    _serialize_messages,
    _serialize_documents,
    _truncate_dict,
    _merge_tags,
    _emit_otel_event,
)
from briefcase.config import BriefcaseConfig, setup


# Fixtures

@pytest.fixture(autouse=True)
def reset_config():
    """Reset the BriefcaseConfig singleton before and after each test."""
    BriefcaseConfig.reset()
    yield
    BriefcaseConfig.reset()


def _make_llm_response(text: str = "Hello") -> MagicMock:
    response = MagicMock()
    response.generations = [[MagicMock(text=text)]]
    response.llm_output = None
    return response


def _make_handler(**kwargs) -> BriefcaseLangChainHandler:
    return BriefcaseLangChainHandler(**kwargs)


# LLM call capture

def test_handler_captures_llm_call():
    """on_llm_start + on_llm_end produce a record with inputs/outputs/latency."""
    handler = _make_handler()
    run_id = str(uuid.uuid4())

    handler.on_llm_start(
        serialized={"kwargs": {"model_name": "gpt-4", "temperature": 0.7}},
        prompts=["What is AI?"],
        run_id=run_id,
    )

    response = _make_llm_response("Artificial Intelligence is...")
    handler.on_llm_end(response, run_id=run_id)

    assert handler.decision_count == 1
    decision = handler.get_decisions()[0]

    assert "prompts" in decision.inputs
    assert len(decision.inputs["prompts"]) == 1
    assert "What is AI?" in decision.inputs["prompts"][0]

    assert "text" in decision.outputs
    assert "Artificial Intelligence" in decision.outputs["text"]

    assert decision.execution_time_ms is not None
    assert decision.execution_time_ms >= 0

    assert decision.decision_type == "llm"
    assert decision.function_name == "gpt-4"


# Chain capture

def test_handler_captures_chain():
    """Chain events capture nested spans."""
    handler = _make_handler()
    chain_run_id = str(uuid.uuid4())
    llm_run_id = str(uuid.uuid4())

    handler.on_chain_start(
        serialized={"id": ["chains", "LLMChain"]},
        inputs={"input": "test query"},
        run_id=chain_run_id,
        parent_run_id=None,
    )

    handler.on_llm_start(
        serialized={"kwargs": {"model_name": "gpt-4"}},
        prompts=["test query"],
        run_id=llm_run_id,
        parent_run_id=chain_run_id,
    )
    handler.on_llm_end(_make_llm_response("Answer"), run_id=llm_run_id)

    handler.on_chain_end(
        outputs={"output": "Answer"},
        run_id=chain_run_id,
        parent_run_id=None,
    )

    assert handler.decision_count == 2

    decisions = handler.get_decisions()
    types = {d.decision_type for d in decisions}
    assert "chain" in types
    assert "llm" in types

    llm_decision = next(d for d in decisions if d.decision_type == "llm")
    assert llm_decision.parent_run_id == chain_run_id

    chain_decision = next(d for d in decisions if d.decision_type == "chain")
    assert chain_decision.parent_run_id is None


# Retriever capture

def test_handler_captures_retriever():
    """on_retriever_end captures documents."""
    handler = _make_handler()
    run_id = str(uuid.uuid4())

    handler.on_retriever_start(
        serialized={"name": "VectorStoreRetriever"},
        query="What is machine learning?",
        run_id=run_id,
    )

    docs = [
        MagicMock(page_content="Machine learning is a subset of AI.", metadata={"source": "wiki"}),
        MagicMock(page_content="ML algorithms learn from data.", metadata={"source": "textbook"}),
    ]
    handler.on_retriever_end(docs, run_id=run_id)

    assert handler.decision_count == 1
    decision = handler.get_decisions()[0]

    assert decision.decision_type == "retriever"
    assert decision.outputs["document_count"] == 2
    assert len(decision.outputs["documents"]) == 2
    assert "content_preview" in decision.outputs["documents"][0]
    assert "Machine learning" in decision.outputs["documents"][0]["content_preview"]


# Tool capture

def test_handler_captures_tool():
    """on_tool_start + on_tool_end capture tool name/input/output."""
    handler = _make_handler()
    run_id = str(uuid.uuid4())

    handler.on_tool_start(
        serialized={"name": "calculator"},
        input_str="2 + 2",
        run_id=run_id,
    )
    handler.on_tool_end(output="4", run_id=run_id)

    assert handler.decision_count == 1
    decision = handler.get_decisions()[0]

    assert decision.decision_type == "tool"
    assert decision.function_name == "calculator"
    assert decision.inputs["input"] == "2 + 2"
    assert decision.outputs["output"] == "4"
    assert decision.execution_time_ms is not None


# Async non-blocking

def test_handler_async_no_block():
    """The handler adds <50ms even with a slow exporter when async_capture=True."""

    async def slow_export_coro(record):
        await asyncio.sleep(0.5)
        return True

    mock_exporter = MagicMock()
    mock_exporter.export = slow_export_coro
    setup(exporter=mock_exporter)

    handler = _make_handler(async_capture=True)
    run_id = str(uuid.uuid4())

    start = time.monotonic()

    handler.on_chain_start(
        serialized={"id": ["chains", "TestChain"]},
        inputs={"input": "test"},
        run_id=run_id,
        parent_run_id=None,
    )
    handler.on_chain_end(
        outputs={"output": "result"},
        run_id=run_id,
        parent_run_id=None,
    )

    elapsed_ms = (time.monotonic() - start) * 1000
    assert elapsed_ms < 50, f"Handler added {elapsed_ms:.1f}ms overhead, expected <50ms"

    assert handler.decision_count == 1
    assert wait_for_pending_exports(5.0)


# Export failure is silent

def test_handler_capture_failure_silent():
    """Export raising does not break chain completion."""

    async def failing_export(record):
        raise RuntimeError("Export service down!")

    mock_exporter = MagicMock()
    mock_exporter.export = failing_export
    setup(exporter=mock_exporter)

    handler = _make_handler(async_capture=False)
    run_id = str(uuid.uuid4())

    # These must NOT raise
    handler.on_chain_start(
        serialized={"id": ["chains", "TestChain"]},
        inputs={"input": "test"},
        run_id=run_id,
        parent_run_id=None,
    )
    handler.on_chain_end(
        outputs={"output": "result"},
        run_id=run_id,
        parent_run_id=None,
    )

    assert handler.decision_count == 1
    assert handler.get_decisions()[0].decision_type == "chain"


# Missing langchain guard

def test_handler_missing_langchain():
    """require_langchain raises ImportError with an install hint."""
    original_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "langchain_core":
            raise ImportError(f"No module named '{name}'")
        return original_import(name, *args, **kwargs)

    builtins.__import__ = mock_import
    try:
        with pytest.raises(ImportError) as exc_info:
            require_langchain()
        error_msg = str(exc_info.value).lower()
        assert "langchain" in error_msg
        assert "pip install" in error_msg
    finally:
        builtins.__import__ = original_import


def test_handler_constructor_missing_langchain(monkeypatch):
    """The constructor raises ImportError when langchain-core is unavailable."""
    import briefcase.integrations.frameworks.langchain_handler as mod
    monkeypatch.setattr(mod, "_LANGCHAIN_AVAILABLE", False)
    with pytest.raises(ImportError, match="pip install"):
        BriefcaseLangChainHandler()


# context_version in decision records

def test_handler_links_context_version():
    """context_version appears in the decision record."""
    handler = _make_handler(context_version="v2.1")
    run_id = str(uuid.uuid4())

    handler.on_llm_start(
        serialized={"kwargs": {"model_name": "gpt-4"}},
        prompts=["Hello"],
        run_id=run_id,
    )
    handler.on_llm_end(_make_llm_response("Hi"), run_id=run_id)

    assert handler.decision_count == 1
    decision = handler.get_decisions()[0]
    assert decision.context_version == "v2.1"

    d = decision.to_dict()
    assert d.get("context_version") == "v2.1"


def test_handler_links_context_version_chain():
    """context_version appears in the assembled chain record."""
    exported = []

    async def capture(record):
        exported.append(record)
        return True

    mock_exporter = MagicMock()
    mock_exporter.export = capture
    setup(exporter=mock_exporter)

    handler = _make_handler(context_version="v3.0", async_capture=False)
    run_id = str(uuid.uuid4())

    handler.on_chain_start(
        serialized={"id": ["chains", "TestChain"]},
        inputs={"input": "x"},
        run_id=run_id,
        parent_run_id=None,
    )
    handler.on_chain_end(outputs={"output": "y"}, run_id=run_id, parent_run_id=None)

    assert len(exported) == 1
    assert exported[0].get("context_version") == "v3.0"


# Exporter called on chain end

def test_handler_calls_exporter():
    """The configured exporter runs on top-level chain end."""
    exported_records = []

    async def capture_export(record):
        exported_records.append(record)
        return True

    mock_exporter = MagicMock()
    mock_exporter.export = capture_export
    setup(exporter=mock_exporter)

    handler = _make_handler(async_capture=False)  # synchronous for determinism
    run_id = str(uuid.uuid4())

    handler.on_chain_start(
        serialized={"id": ["chains", "TestChain"]},
        inputs={"input": "test"},
        run_id=run_id,
        parent_run_id=None,
    )
    handler.on_chain_end(
        outputs={"output": "result"},
        run_id=run_id,
        parent_run_id=None,
    )

    assert len(exported_records) == 1
    record = exported_records[0]
    assert record["decision_type"] == "chain"
    assert record["function_name"] == "TestChain"
    assert "child_spans" in record


def test_exported_record_uses_wire_field_names():
    """Exported chain records carry the decision-record core field names."""
    exported_records = []

    async def capture_export(record):
        exported_records.append(record)
        return True

    mock_exporter = MagicMock()
    mock_exporter.export = capture_export
    setup(exporter=mock_exporter)

    handler = _make_handler(async_capture=False)
    run_id = str(uuid.uuid4())
    handler.on_chain_start(
        serialized={"id": ["chains", "WireChain"]}, inputs={"input": "x"},
        run_id=run_id, parent_run_id=None,
    )
    handler.on_chain_end(outputs={"output": "y"}, run_id=run_id, parent_run_id=None)

    record = exported_records[0]
    for key in ("decision_id", "decision_type", "function_name", "inputs",
                "outputs", "started_at", "ended_at", "execution_time_ms"):
        assert key in record, key


def test_handler_calls_exporter_only_on_toplevel():
    """The exporter is NOT called for a nested (child) chain."""
    exported_records = []

    async def capture_export(record):
        exported_records.append(record)
        return True

    mock_exporter = MagicMock()
    mock_exporter.export = capture_export
    setup(exporter=mock_exporter)

    handler = _make_handler(async_capture=False)
    parent_id = str(uuid.uuid4())
    child_id = str(uuid.uuid4())

    handler.on_chain_start(
        serialized={"id": ["chains", "Parent"]},
        inputs={"input": "x"},
        run_id=parent_id,
        parent_run_id=None,
    )
    handler.on_chain_start(
        serialized={"id": ["chains", "Child"]},
        inputs={"input": "y"},
        run_id=child_id,
        parent_run_id=parent_id,
    )
    # End the child first (has a parent, no export)
    handler.on_chain_end(outputs={"o": "1"}, run_id=child_id, parent_run_id=parent_id)
    assert len(exported_records) == 0  # no export yet

    # End the parent (top level, export triggered)
    handler.on_chain_end(outputs={"o": "2"}, run_id=parent_id, parent_run_id=None)
    assert len(exported_records) == 1
    assert exported_records[0]["function_name"] == "Parent"


# Constructor defaults and full lifecycle

def test_handler_constructor_defaults():
    """All constructor parameters have working defaults."""
    handler = BriefcaseLangChainHandler(
        capture_llm=True,
        capture_chains=True,
        capture_tools=True,
        capture_retrievers=True,
        max_input_chars=5000,
        max_output_chars=5000,
    )

    assert handler.context_version is None
    assert handler.async_capture is True
    assert handler._exporter is None

    # Full lifecycle works
    run_id = str(uuid.uuid4())
    handler.on_chain_start(
        serialized={"id": ["chains", "LLMChain"]},
        inputs={"input": "test"},
        run_id=run_id,
    )
    handler.on_chain_end(outputs={"output": "result"}, run_id=run_id)
    assert handler.decision_count == 1

    tool_id = str(uuid.uuid4())
    handler.on_tool_start(
        serialized={"name": "search"},
        input_str="query",
        run_id=tool_id,
    )
    handler.on_tool_end(output="results", run_id=tool_id)
    assert handler.decision_count == 2

    ret_id = str(uuid.uuid4())
    handler.on_retriever_start(
        serialized={"name": "retriever"},
        query="q",
        run_id=ret_id,
    )
    handler.on_retriever_end(
        documents=[MagicMock(page_content="doc", metadata={})],
        run_id=ret_id,
    )
    assert handler.decision_count == 3


# CapturedDecision dict serialization

def test_captured_decision_to_dict_with_error():
    """to_dict() includes the error field when set."""
    d = CapturedDecision(
        decision_id="d1", decision_type="llm", function_name="gpt-4",
        error="API timeout",
    ).to_dict()
    assert d["error"] == "API timeout"


def test_captured_decision_to_dict_with_token_usage():
    """to_dict() includes token_usage when set."""
    d = CapturedDecision(
        decision_id="d1", decision_type="llm", function_name="gpt-4",
        token_usage={"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15},
    ).to_dict()
    assert d["token_usage"]["total_tokens"] == 15


# Public API methods

def test_get_decisions_as_dicts_returns_list_of_dicts():
    """get_decisions_as_dicts() returns serializable dicts."""
    handler = _make_handler()
    run_id = str(uuid.uuid4())
    handler.on_llm_start(
        serialized={"kwargs": {"model_name": "gpt-4"}},
        prompts=["Hello"],
        run_id=run_id,
    )
    handler.on_llm_end(_make_llm_response("Hi"), run_id=run_id)

    dicts = handler.get_decisions_as_dicts()
    assert len(dicts) == 1
    assert isinstance(dicts[0], dict)
    assert dicts[0]["decision_type"] == "llm"


def test_clear_removes_all_state():
    """clear() wipes decisions and inflight state."""
    handler = _make_handler()
    run_id = str(uuid.uuid4())
    handler.on_chain_start(
        serialized={"id": ["chains", "X"]}, inputs={}, run_id=run_id,
    )
    handler.on_chain_end(outputs={}, run_id=run_id)
    assert handler.decision_count == 1

    handler._inflight["fly"] = CapturedDecision("fly", "llm", "m")
    handler.clear()
    assert handler.decision_count == 0
    assert len(handler._inflight) == 0


# capture=False flags

def test_capture_llm_false_skips_llm():
    """capture_llm=False: on_llm_start creates nothing."""
    handler = _make_handler(capture_llm=False)
    handler.on_llm_start(
        serialized={"kwargs": {}}, prompts=["x"], run_id="r1",
    )
    assert "r1" not in handler._inflight


def test_capture_chains_false_skips_chain():
    """capture_chains=False: on_chain_start creates nothing."""
    handler = _make_handler(capture_chains=False)
    handler.on_chain_start(
        serialized={"id": ["chains", "C"]}, inputs={}, run_id="r1",
    )
    assert "r1" not in handler._inflight


def test_capture_tools_false_skips_tool():
    """capture_tools=False: on_tool_start creates nothing."""
    handler = _make_handler(capture_tools=False)
    handler.on_tool_start(
        serialized={"name": "calc"}, input_str="1+1", run_id="r1",
    )
    assert "r1" not in handler._inflight


def test_capture_retrievers_false_skips_retriever():
    """capture_retrievers=False: on_retriever_start creates nothing."""
    handler = _make_handler(capture_retrievers=False)
    handler.on_retriever_start(
        serialized={"name": "ret"}, query="q", run_id="r1",
    )
    assert "r1" not in handler._inflight


# Error callbacks

def test_on_llm_error_captures_error():
    """on_llm_error records the error string."""
    handler = _make_handler()
    run_id = str(uuid.uuid4())
    handler.on_llm_start(
        serialized={"kwargs": {"model_name": "gpt-4"}},
        prompts=["Hello"],
        run_id=run_id,
    )
    handler.on_llm_error(Exception("Timeout"), run_id=run_id)
    assert handler.decision_count == 1
    assert handler.get_decisions()[0].error == "Timeout"


def test_on_llm_error_unknown_run_id_is_noop():
    """on_llm_error with an unknown run_id does nothing."""
    handler = _make_handler()
    handler.on_llm_error(Exception("x"), run_id="unknown")
    assert handler.decision_count == 0


def test_on_chain_error_captures_error():
    """on_chain_error records the error string."""
    handler = _make_handler()
    run_id = str(uuid.uuid4())
    handler.on_chain_start(
        serialized={"id": ["chains", "C"]}, inputs={}, run_id=run_id,
    )
    handler.on_chain_error(ValueError("bad"), run_id=run_id)
    assert handler.decision_count == 1
    assert "bad" in handler.get_decisions()[0].error


def test_on_chain_error_unknown_run_id_is_noop():
    """on_chain_error with an unknown run_id does nothing."""
    handler = _make_handler()
    handler.on_chain_error(Exception("x"), run_id="unknown")
    assert handler.decision_count == 0


def test_on_tool_error_captures_error():
    """on_tool_error records the error string."""
    handler = _make_handler()
    run_id = str(uuid.uuid4())
    handler.on_tool_start(
        serialized={"name": "search"}, input_str="query", run_id=run_id,
    )
    handler.on_tool_error(Exception("Tool failed"), run_id=run_id)
    assert handler.decision_count == 1
    assert "Tool failed" in handler.get_decisions()[0].error


def test_on_tool_error_unknown_run_id_is_noop():
    """on_tool_error with an unknown run_id does nothing."""
    handler = _make_handler()
    handler.on_tool_error(Exception("x"), run_id="unknown")
    assert handler.decision_count == 0


def test_on_retriever_error_captures_error():
    """on_retriever_error records the error string."""
    handler = _make_handler()
    run_id = str(uuid.uuid4())
    handler.on_retriever_start(
        serialized={"name": "ret"}, query="q", run_id=run_id,
    )
    handler.on_retriever_error(Exception("Retrieval failed"), run_id=run_id)
    assert handler.decision_count == 1
    assert "Retrieval failed" in handler.get_decisions()[0].error


def test_on_retriever_error_unknown_run_id_is_noop():
    """on_retriever_error with an unknown run_id does nothing."""
    handler = _make_handler()
    handler.on_retriever_error(Exception("x"), run_id="unknown")
    assert handler.decision_count == 0


# Early returns (unknown run_id)

def test_on_llm_end_unknown_run_id_is_noop():
    """on_llm_end with an unknown run_id does nothing."""
    handler = _make_handler()
    handler.on_llm_end(_make_llm_response(), run_id="unknown_123")
    assert handler.decision_count == 0


def test_on_chain_end_unknown_run_id_is_noop():
    """on_chain_end with an unknown run_id does nothing."""
    handler = _make_handler()
    handler.on_chain_end(outputs={}, run_id="unknown_456")
    assert handler.decision_count == 0


def test_on_tool_end_unknown_run_id_is_noop():
    """on_tool_end with an unknown run_id does nothing."""
    handler = _make_handler()
    handler.on_tool_end("result", run_id="unknown_789")
    assert handler.decision_count == 0


def test_on_retriever_end_unknown_run_id_is_noop():
    """on_retriever_end with an unknown run_id does nothing."""
    handler = _make_handler()
    handler.on_retriever_end([], run_id="unknown_abc")
    assert handler.decision_count == 0


# Chat model start

def test_on_chat_model_start_creates_inflight():
    """on_chat_model_start creates an inflight LLM decision."""
    handler = _make_handler()
    run_id = str(uuid.uuid4())
    messages = [[MagicMock(type="human", content="Hello")]]
    handler.on_chat_model_start(
        serialized={"kwargs": {"model_name": "gpt-4"}},
        messages=messages,
        run_id=run_id,
    )
    assert run_id in handler._inflight
    assert handler._inflight[run_id].decision_type == "llm"
    assert "messages" in handler._inflight[run_id].inputs


def test_on_chat_model_start_capture_llm_false():
    """on_chat_model_start respects capture_llm=False."""
    handler = _make_handler(capture_llm=False)
    run_id = str(uuid.uuid4())
    handler.on_chat_model_start(
        serialized={"kwargs": {}},
        messages=[[MagicMock(type="human", content="x")]],
        run_id=run_id,
    )
    assert run_id not in handler._inflight


def test_on_chat_model_start_then_llm_end():
    """on_chat_model_start followed by on_llm_end completes the lifecycle."""
    handler = _make_handler(context_version="v1.0")
    run_id = str(uuid.uuid4())
    messages = [[MagicMock(type="human", content="Question?")]]
    handler.on_chat_model_start(
        serialized={"kwargs": {"model_name": "claude-3"}},
        messages=messages,
        run_id=run_id,
    )
    handler.on_llm_end(_make_llm_response("Answer"), run_id=run_id)
    assert handler.decision_count == 1
    d = handler.get_decisions()[0]
    assert d.context_version == "v1.0"
    assert "messages" in d.inputs


# No-op callbacks

def test_noop_callbacks_do_not_raise():
    """on_llm_new_token, on_text, on_agent_action, on_agent_finish all pass."""
    handler = _make_handler()
    handler.on_llm_new_token("token")
    handler.on_text("some text")
    handler.on_agent_action(MagicMock())
    handler.on_agent_finish(MagicMock())
    assert handler.decision_count == 0


# Exception handling silences errors

def test_on_llm_start_exception_is_silent():
    """datetime.now raising inside on_llm_start is silently caught."""
    handler = _make_handler()
    import briefcase.integrations.frameworks.langchain_handler as mod
    with patch.object(mod, 'datetime') as mock_dt:
        mock_dt.now.side_effect = RuntimeError("datetime broken")
        mock_dt.timezone = timezone
        handler.on_llm_start(serialized={}, prompts=["x"], run_id="test_err")
    assert "test_err" not in handler._inflight


def test_on_chain_start_exception_is_silent():
    """_truncate_dict raising inside on_chain_start is silently caught."""
    handler = _make_handler()
    import briefcase.integrations.frameworks.langchain_handler as mod
    with patch.object(mod, '_truncate_dict', side_effect=RuntimeError("trunc error")):
        handler.on_chain_start(
            serialized={"id": ["chains", "X"]}, inputs={"k": "v"}, run_id="test_err2",
        )
    assert "test_err2" not in handler._inflight


def test_on_tool_start_exception_is_silent():
    """An internal error in the on_tool_start body is silently caught."""
    handler = _make_handler()
    import briefcase.integrations.frameworks.langchain_handler as mod
    with patch.object(mod, '_merge_tags', side_effect=RuntimeError("tags error")):
        handler.on_tool_start(
            serialized={"name": "t"}, input_str="x", run_id="test_err3",
        )
    assert "test_err3" not in handler._inflight


def test_trigger_export_exception_is_silent():
    """_trigger_export swallows a BriefcaseConfig failure."""
    handler = _make_handler(async_capture=False)
    with patch("briefcase.config.BriefcaseConfig.get", side_effect=RuntimeError("cfg broken")):
        decision = CapturedDecision("d1", "chain", "C")
        handler._trigger_export(decision)  # must not raise


# Helper function coverage

def test_extract_model_name_from_direct_kwargs():
    """_extract_model_name reads from direct kwargs."""
    result = _extract_model_name({}, {"model_name": "gpt-4"})
    assert result == "gpt-4"


def test_extract_model_name_from_serialized_kwargs():
    """_extract_model_name reads from serialized.kwargs."""
    result = _extract_model_name({"kwargs": {"model": "text-ada"}}, {})
    assert result == "text-ada"


def test_extract_model_name_from_id_path():
    """_extract_model_name returns the last element of the id list."""
    result = _extract_model_name({"id": ["openai", "ChatOpenAI"]}, {})
    assert result == "ChatOpenAI"


def test_extract_model_name_from_name_fallback():
    """_extract_model_name falls back to serialized.name."""
    result = _extract_model_name({"name": "my-model"}, {})
    assert result == "my-model"


def test_extract_model_name_unknown():
    """_extract_model_name returns 'unknown_model' when nothing is found."""
    result = _extract_model_name({}, {})
    assert result == "unknown_model"


def test_extract_model_params_from_serialized():
    """_extract_model_params reads from serialized.kwargs."""
    result = _extract_model_params(
        {"kwargs": {"temperature": 0.5, "max_tokens": 100}}, {}
    )
    assert result["temperature"] == 0.5
    assert result["max_tokens"] == 100


def test_extract_model_params_from_direct_kwargs():
    """_extract_model_params falls back to direct kwargs."""
    result = _extract_model_params({}, {"temperature": 0.9, "model": "gpt-4"})
    assert result["temperature"] == 0.9
    assert result["model"] == "gpt-4"


def test_extract_chain_name_from_id():
    """_extract_chain_name reads the last element of the id list."""
    result = _extract_chain_name({"id": ["chains", "LLMChain"]})
    assert result == "LLMChain"


def test_extract_chain_name_from_name_fallback():
    """_extract_chain_name falls back to the name field."""
    result = _extract_chain_name({"name": "MyChain"})
    assert result == "MyChain"


def test_extract_chain_name_unknown():
    """_extract_chain_name returns 'unknown_chain' when nothing is found."""
    result = _extract_chain_name({})
    assert result == "unknown_chain"


def test_extract_llm_output_none_response():
    """_extract_llm_output handles a None response."""
    text, usage = _extract_llm_output(None)
    assert text == ""
    assert usage is None


def test_extract_llm_output_with_text():
    """_extract_llm_output extracts text from the .text attribute."""
    response = MagicMock()
    response.generations = [[MagicMock(text="Hello World")]]
    response.llm_output = None
    text, usage = _extract_llm_output(response)
    assert text == "Hello World"
    assert usage is None


def test_extract_llm_output_with_chat_message_content():
    """_extract_llm_output extracts content from .message.content."""
    gen = MagicMock(spec=[])  # no .text attribute
    gen.message = MagicMock()
    gen.message.content = "Chat response"
    response = MagicMock()
    response.generations = [[gen]]
    response.llm_output = None
    text, usage = _extract_llm_output(response)
    assert text == "Chat response"


def test_extract_llm_output_with_token_usage():
    """_extract_llm_output extracts token usage from llm_output."""
    response = MagicMock()
    response.generations = [[MagicMock(text="x")]]
    response.llm_output = {
        "token_usage": {"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15}
    }
    text, usage = _extract_llm_output(response)
    assert usage["total_tokens"] == 15
    assert usage["prompt_tokens"] == 5


def test_serialize_messages_with_typed_messages():
    """_serialize_messages handles messages with .type and .content."""
    msg = MagicMock()
    msg.type = "human"
    msg.content = "Hello"
    result = _serialize_messages([[msg]], max_chars=100)
    assert result[0]["role"] == "human"
    assert result[0]["content"] == "Hello"


def test_serialize_messages_with_dict_messages():
    """_serialize_messages handles dict-format messages."""
    result = _serialize_messages(
        [[{"role": "assistant", "content": "Hi there"}]], max_chars=100
    )
    assert result[0]["role"] == "assistant"
    assert result[0]["content"] == "Hi there"


def test_serialize_messages_with_plain_string():
    """_serialize_messages handles unknown message types."""
    result = _serialize_messages([["plain string"]], max_chars=100)
    assert result[0]["role"] == "unknown"
    assert "plain string" in result[0]["content"]


def test_serialize_messages_non_list_input():
    """_serialize_messages handles non-list message input."""
    msg = MagicMock()
    msg.type = "system"
    msg.content = "Be helpful"
    result = _serialize_messages([msg], max_chars=100)
    assert len(result) >= 1


def test_serialize_documents_with_page_content():
    """_serialize_documents handles objects with page_content + metadata."""
    doc = MagicMock()
    doc.page_content = "Important text"
    doc.metadata = {"source": "wiki"}
    result = _serialize_documents([doc], max_chars=1000)
    assert result[0]["content_preview"] == "Important text"
    assert result[0]["metadata"]["source"] == "wiki"


def test_serialize_documents_with_dict():
    """_serialize_documents handles dict-format documents."""
    result = _serialize_documents(
        [{"page_content": "Dict content", "metadata": {"k": "v"}}], max_chars=1000
    )
    assert "Dict content" in result[0]["content_preview"]


def test_serialize_documents_with_content_key():
    """_serialize_documents handles dicts with a 'content' key."""
    result = _serialize_documents(
        [{"content": "Content key text"}], max_chars=1000
    )
    assert "Content key text" in result[0]["content_preview"]


def test_serialize_documents_with_unknown_type():
    """_serialize_documents handles unknown document types."""
    result = _serialize_documents(["just a string"], max_chars=1000)
    assert "just a string" in result[0]["content_preview"]


def test_serialize_documents_none():
    """_serialize_documents handles None input."""
    result = _serialize_documents(None, max_chars=1000)
    assert result == []


def test_truncate_dict_with_string_values():
    """_truncate_dict truncates string values."""
    result = _truncate_dict({"key": "a" * 100}, max_chars=10)
    assert len(result["key"]) == 10


def test_truncate_dict_with_nested_dict():
    """_truncate_dict recurses into nested dicts."""
    result = _truncate_dict({"outer": {"inner": "b" * 100}}, max_chars=5)
    assert len(result["outer"]["inner"]) == 5


def test_truncate_dict_with_non_string_value():
    """_truncate_dict passes through non-string values."""
    result = _truncate_dict({"num": 42, "lst": [1, 2]}, max_chars=5)
    assert result["num"] == 42
    assert result["lst"] == [1, 2]


def test_truncate_dict_with_non_dict_input():
    """_truncate_dict wraps non-dict inputs."""
    result = _truncate_dict("raw string", max_chars=5)
    assert result == {"value": "raw s"}


def test_merge_tags_with_tags_only():
    """_merge_tags merges a tag list."""
    result = _merge_tags(["tagA", "tagB"], None)
    assert result["tag_0"] == "tagA"
    assert result["tag_1"] == "tagB"


def test_merge_tags_with_metadata_only():
    """_merge_tags merges a metadata dict."""
    result = _merge_tags(None, {"env": "prod", "version": "1"})
    assert result["env"] == "prod"
    assert result["version"] == "1"


def test_merge_tags_combined():
    """_merge_tags merges both tags and metadata."""
    result = _merge_tags(["t1"], {"k": "v"})
    assert result["tag_0"] == "t1"
    assert result["k"] == "v"


def test_merge_tags_empty():
    """_merge_tags handles None/None."""
    result = _merge_tags(None, None)
    assert result == {}


def test_emit_otel_event_does_not_raise():
    """_emit_otel_event runs without error when no span is active."""
    _emit_otel_event("test_event", {"key": "value"})  # must not raise


# Assemble decision record with children

def test_assemble_decision_record_includes_child_spans():
    """_assemble_decision_record collects child decisions."""
    exported = []

    async def capture(record):
        exported.append(record)
        return True

    mock_exporter = MagicMock()
    mock_exporter.export = capture
    setup(exporter=mock_exporter)

    handler = _make_handler(async_capture=False)
    parent_id = str(uuid.uuid4())
    llm_id = str(uuid.uuid4())

    handler.on_chain_start(
        serialized={"id": ["chains", "LLMChain"]},
        inputs={"input": "hello"},
        run_id=parent_id,
        parent_run_id=None,
    )
    handler.on_llm_start(
        serialized={"kwargs": {"model_name": "gpt-4"}},
        prompts=["hello"],
        run_id=llm_id,
        parent_run_id=parent_id,
    )
    handler.on_llm_end(_make_llm_response("world"), run_id=llm_id)
    handler.on_chain_end(outputs={"output": "world"}, run_id=parent_id, parent_run_id=None)

    assert len(exported) == 1
    record = exported[0]
    assert record["decision_type"] == "chain"
    assert "child_spans" in record
    assert len(record["child_spans"]) == 1
    assert record["child_spans"][0]["decision_type"] == "llm"


# Async capture background path

def test_trigger_export_async_background_thread():
    """async_capture=True exports in the background without blocking."""
    completed = []

    async def export_fn(record):
        completed.append(record)
        return True

    mock_exporter = MagicMock()
    mock_exporter.export = export_fn
    setup(exporter=mock_exporter)

    handler = _make_handler(async_capture=True)
    run_id = str(uuid.uuid4())
    handler.on_chain_start(
        serialized={"id": ["chains", "BG"]}, inputs={}, run_id=run_id, parent_run_id=None
    )
    handler.on_chain_end(outputs={}, run_id=run_id, parent_run_id=None)

    assert wait_for_pending_exports(5.0)
    assert len(completed) == 1


def test_trigger_export_async_background_error_is_silent():
    """Background export errors do not propagate."""
    async def failing_export(record):
        raise RuntimeError("background failure")

    mock_exporter = MagicMock()
    mock_exporter.export = failing_export
    setup(exporter=mock_exporter)

    handler = _make_handler(async_capture=True)
    run_id = str(uuid.uuid4())
    handler.on_chain_start(
        serialized={"id": ["chains", "Fail"]}, inputs={}, run_id=run_id, parent_run_id=None
    )
    handler.on_chain_end(outputs={}, run_id=run_id, parent_run_id=None)

    assert wait_for_pending_exports(5.0)
    # No exception surfaced in the main thread
    assert handler.decision_count == 1


# Tool serialized via id path

def test_tool_name_from_id_list():
    """on_tool_start extracts the name from the serialized.id list."""
    handler = _make_handler()
    run_id = str(uuid.uuid4())
    handler.on_tool_start(
        serialized={"id": ["tools", "SearchTool"]},
        input_str="query",
        run_id=run_id,
    )
    assert handler._inflight[run_id].function_name == "SearchTool"


def test_retriever_name_fallback():
    """on_retriever_start falls back to 'retriever' when no name is given."""
    handler = _make_handler()
    run_id = str(uuid.uuid4())
    handler.on_retriever_start(
        serialized={},  # no name
        query="q",
        run_id=run_id,
    )
    assert handler._inflight[run_id].function_name == "retriever"
