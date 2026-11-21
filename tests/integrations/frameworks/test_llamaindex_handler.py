"""Tests for briefcase/integrations/frameworks/llamaindex_handler.py.

The llama_index package is stubbed by conftest.py; the handler is
duck-typed, so the tests drive on_event_start / on_event_end directly with
fake payloads.
"""

from types import SimpleNamespace

import pytest

from briefcase.config import BriefcaseConfig
from briefcase.integrations.frameworks import llamaindex_handler
from briefcase.integrations.frameworks.llamaindex_handler import (
    BriefcaseLlamaIndexHandler,
    EventType,
    _serialize_payload_messages,
)


@pytest.fixture(autouse=True)
def reset_config():
    BriefcaseConfig.reset()
    yield
    BriefcaseConfig.reset()


class _Exporter:
    def __init__(self):
        self.records = []

    async def export(self, record):
        self.records.append(record)


def _handler(**kwargs):
    exporter = _Exporter()
    handler = BriefcaseLlamaIndexHandler(exporter=exporter, async_capture=False, **kwargs)
    return handler, exporter


# Construction

def test_requires_llama_index(monkeypatch):
    monkeypatch.setattr(llamaindex_handler, "_LLAMAINDEX_AVAILABLE", False)
    with pytest.raises(ImportError, match=r"briefcase-ai\[llamaindex\]"):
        BriefcaseLlamaIndexHandler()


def test_callback_interface_attributes():
    handler = BriefcaseLlamaIndexHandler(
        event_starts_to_ignore=["chunking"], event_ends_to_ignore=["chunking"],
    )
    assert handler.event_starts_to_ignore == ["chunking"]
    assert handler.event_ends_to_ignore == ["chunking"]
    assert handler.decision_count == 0
    assert handler.get_decisions() == []


# LLM events

def test_llm_event_captures_messages_output_and_tokens():
    handler, exporter = _handler(context_version="v7")
    event_id = handler.on_event_start(
        EventType.LLM,
        payload={
            "model_name": "gpt-4o",
            "messages": [SimpleNamespace(role="user", content="What is AI?")],
            "template": "Answer: {q}",
            "model_dict": {"temperature": 0.2, "max_tokens": 50, "ignored": 1},
        },
        event_id="e1",
        parent_id="root",
    )
    assert event_id == "e1"
    assert "e1" in handler._inflight

    handler.on_event_end(
        EventType.LLM,
        payload={
            "response": SimpleNamespace(text="AI is..."),
            "token_count": SimpleNamespace(
                prompt_tokens=5, completion_tokens=3, total_tokens=8
            ),
        },
        event_id="e1",
    )

    decision = handler.get_decisions()[0]
    assert decision.decision_type == "llm"
    assert decision.function_name == "gpt-4o"
    assert decision.inputs == {
        "messages": [{"role": "user", "content": "What is AI?"}],
        "template": "Answer: {q}",
    }
    assert decision.outputs == {"text": "AI is..."}
    assert decision.model_parameters == {"temperature": 0.2, "max_tokens": 50}
    assert decision.token_usage == {
        "prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8,
    }
    assert decision.parent_run_id == "root"
    assert decision.context_version == "v7"
    assert decision.execution_time_ms is not None and decision.execution_time_ms >= 0
    assert len(handler._inflight) == 0
    # LLM events are not exported on their own.
    assert exporter.records == []


def test_llm_response_shapes_and_integer_token_count():
    handler, _ = _handler(max_output_chars=4)
    handler.on_event_start(EventType.LLM, {"model_dict": {"model": "m"}}, event_id="a")
    handler.on_event_end(
        EventType.LLM,
        {"response": SimpleNamespace(message=SimpleNamespace(content="chat reply")),
         "token_count": 12},
        event_id="a",
    )
    handler.on_event_start(EventType.LLM, {}, event_id="b")
    handler.on_event_end(EventType.LLM, {"response": "plain string"}, event_id="b")

    first, second = handler.get_decisions()
    assert first.function_name == "m"
    assert first.outputs == {"text": "chat"}  # truncated to max_output_chars
    assert first.token_usage == {
        "prompt_tokens": 12, "completion_tokens": 0, "total_tokens": 12,
    }
    assert second.function_name == "unknown_llm"
    assert second.outputs == {"text": "plai"}
    assert second.token_usage is None


# Embedding / retrieval / query / synthesize

def test_embedding_event():
    handler, _ = _handler()
    handler.on_event_start(
        EventType.EMBEDDING,
        {"model_name": "embed-3", "texts": ["a", "b"], "serialized": {"dim": 3}},
        event_id="e",
    )
    handler.on_event_end(
        EventType.EMBEDDING, {"chunks": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]}, event_id="e"
    )
    decision = handler.get_decisions()[0]
    assert decision.decision_type == "embedding"
    assert decision.function_name == "embed-3"
    assert decision.inputs == {"text_count": 2}
    assert decision.outputs == {"embedding_count": 2, "dimensions": 3}
    assert decision.model_parameters == {"dim": 3}


def test_retrieve_event_serializes_all_node_shapes():
    handler, _ = _handler()
    handler.on_event_start(EventType.RETRIEVE, {"query_str": "find it"}, event_id="r")
    handler.on_event_end(
        EventType.RETRIEVE,
        {"nodes": [
            SimpleNamespace(text="direct text", score=0.9),
            SimpleNamespace(node=SimpleNamespace(text="wrapped text"), score=0.8),
            {"text": "dict text", "score": 0.7},
            object(),  # unsupported shape is skipped
        ]},
        event_id="r",
    )
    decision = handler.get_decisions()[0]
    assert decision.decision_type == "retriever"
    assert decision.inputs == {"query": "find it"}
    assert decision.outputs["document_count"] == 3
    assert decision.outputs["documents"] == [
        {"content_preview": "direct text", "score": 0.9},
        {"content_preview": "wrapped text", "score": 0.8},
        {"content_preview": "dict text", "score": 0.7},
    ]


def test_query_event_is_exported():
    handler, exporter = _handler()
    handler.on_event_start(EventType.QUERY, {"query_str": "why?"}, event_id="q")
    handler.on_event_end(
        EventType.QUERY, {"response": SimpleNamespace(response="because")}, event_id="q"
    )
    assert len(exporter.records) == 1
    record = exporter.records[0]
    assert record["decision_type"] == "query"
    assert record["function_name"] == "query_engine"
    assert record["inputs"] == {"query": "why?"}
    assert record["outputs"] == {"response": "because"}
    assert handler.get_decisions_as_dicts() == [record]


def test_synthesize_event_recorded_but_not_exported():
    handler, exporter = _handler()
    handler.on_event_start(EventType.SYNTHESIZE, {"query_str": "why?"}, event_id="s")
    handler.on_event_end(EventType.SYNTHESIZE, {"response": "text answer"}, event_id="s")
    decision = handler.get_decisions()[0]
    assert decision.decision_type == "synthesize"
    assert decision.outputs == {"response": "text answer"}
    assert exporter.records == []


# Exceptions

def test_exception_event_marks_matching_decision_and_exports_failed_query():
    handler, exporter = _handler()
    handler.on_event_start(EventType.QUERY, {"query_str": "q"}, event_id="q")
    handler.on_event_end(
        EventType.EXCEPTION, {"exception": RuntimeError("index offline")}, event_id="q"
    )
    decision = handler.get_decisions()[0]
    assert decision.error == "index offline"
    assert decision.ended_at is not None
    assert [r["error"] for r in exporter.records] == ["index offline"]


def test_exception_event_falls_back_to_latest_inflight():
    handler, exporter = _handler()
    handler.on_event_start(EventType.LLM, {}, event_id="first")
    handler.on_event_start(EventType.LLM, {}, event_id="second")
    handler.on_event_end(EventType.EXCEPTION, {"error": "boom"}, event_id="unknown")

    assert [d.decision_id for d in handler.get_decisions()] == ["second"]
    assert handler.get_decisions()[0].error == "boom"
    assert list(handler._inflight) == ["first"]
    assert exporter.records == []  # a failed LLM event is not a query

    # No in-flight decision at all: nothing happens.
    handler.clear()
    handler.on_event_end(EventType.EXCEPTION, {}, event_id="none")
    assert handler.decision_count == 0


# Filtering and bookkeeping

def test_capture_flags_and_ignore_lists():
    handler, _ = _handler(
        capture_llm=False, capture_embeddings=False, capture_retrievals=False,
        capture_queries=False,
    )
    for event_type in (EventType.LLM, EventType.EMBEDDING, EventType.RETRIEVE,
                       EventType.QUERY, EventType.SYNTHESIZE):
        handler.on_event_start(event_type, {}, event_id=event_type)
        handler.on_event_end(event_type, {}, event_id=event_type)
    assert handler.decision_count == 0 and len(handler._inflight) == 0

    ignoring, _ = _handler(event_starts_to_ignore=[EventType.LLM],
                           event_ends_to_ignore=[EventType.QUERY])
    assert ignoring.on_event_start(EventType.LLM, {}, event_id="x") == "x"
    assert len(ignoring._inflight) == 0
    ignoring.on_event_start(EventType.QUERY, {}, event_id="q")
    ignoring.on_event_end(EventType.QUERY, {}, event_id="q")
    assert ignoring.decision_count == 0 and "q" in ignoring._inflight


def test_event_id_generated_and_unknown_end_ignored():
    handler, _ = _handler()
    generated = handler.on_event_start(EventType.LLM, None)
    assert generated and generated in handler._inflight
    handler.on_event_end(EventType.LLM, None, event_id="never-started")
    handler.on_event_end(EventType.CHUNKING, {}, event_id=generated)
    assert handler.decision_count == 0


def test_trace_lifecycle_and_clear():
    handler, _ = _handler()
    handler.start_trace("t1")
    assert handler._current_trace_id == "t1" and "t1" in handler._trace_map
    handler.end_trace("t1")
    assert handler._trace_map == {} and handler._current_trace_id is None

    handler.start_trace()
    assert handler._current_trace_id in handler._trace_map
    handler.end_trace()
    assert handler._trace_map == {}

    handler.start_trace("t2")
    handler.on_event_start(EventType.LLM, {}, event_id="e")
    handler.clear()
    assert handler.decision_count == 0
    assert len(handler._inflight) == 0 and handler._trace_map == {}


def test_serialize_payload_messages_shapes():
    assert _serialize_payload_messages(
        [SimpleNamespace(role="system", content="abcdef"), {"content": "dict"}, 42], 3
    ) == [
        {"role": "system", "content": "abc"},
        {"role": "unknown", "content": "dic"},
        {"role": "unknown", "content": "42"},
    ]
    assert _serialize_payload_messages("single", 10) == [
        {"role": "unknown", "content": "single"}
    ]


# Bounds and content options

def test_decisions_are_a_ring_buffer():
    handler, exporter = _handler(max_records=2)
    for i in range(5):
        handler.on_event_start(EventType.QUERY, {"query_str": str(i)}, event_id=f"q{i}")
        handler.on_event_end(EventType.QUERY, {"response": "r"}, event_id=f"q{i}")
    assert [d.decision_id for d in handler.get_decisions()] == ["q3", "q4"]
    assert len(exporter.records) == 5


def test_unfinished_events_are_abandoned_not_leaked():
    handler, exporter = _handler(max_inflight=2)
    handler.on_event_start(EventType.QUERY, {"query_str": "lost"}, event_id="q0")
    handler.on_event_start(EventType.LLM, {}, event_id="l1")
    handler.on_event_start(EventType.LLM, {}, event_id="l2")
    handler.on_event_start(EventType.LLM, {}, event_id="l3")

    assert list(handler._inflight) == ["l2", "l3"]
    abandoned = handler.get_decisions()
    assert [d.decision_id for d in abandoned] == ["q0", "l1"]
    assert all(d.error.startswith("abandoned") for d in abandoned)
    # The abandoned query is exported like any other failed query.
    assert [r["decision_id"] for r in exporter.records] == ["q0"]


def test_redact_and_capture_content():
    handler, exporter = _handler(redact=lambda t: t.replace("alice@example.com", "[EMAIL]"))
    handler.on_event_start(
        EventType.QUERY, {"query_str": "mail alice@example.com"}, event_id="q"
    )
    handler.on_event_end(
        EventType.QUERY, {"response": "sent to alice@example.com"}, event_id="q"
    )
    assert exporter.records[0]["inputs"] == {"query": "mail [EMAIL]"}
    assert exporter.records[0]["outputs"] == {"response": "sent to [EMAIL]"}
    assert "alice@example.com" not in repr(handler.get_decisions_as_dicts())

    none_handler, none_exporter = _handler(capture_content="none")
    none_handler.on_event_start(EventType.QUERY, {"query_str": "secret"}, event_id="q")
    none_handler.on_event_end(
        EventType.EXCEPTION, {"exception": ValueError("secret")}, event_id="q"
    )
    record = none_exporter.records[0]
    assert record["inputs"] == {} and record["outputs"] == {}
    assert record["error"] == "<omitted>"
