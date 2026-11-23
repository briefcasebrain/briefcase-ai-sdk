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
    # These tests check what the handler extracts, so they opt in to content.
    kwargs.setdefault("capture_content", "full")
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
    # A standalone LLM call (the callback manager's "root" parent) is exported.
    assert [r["decision_id"] for r in exporter.records] == ["e1"]
    assert exporter.records[0]["token_usage"] == decision.token_usage
    assert exporter.records[0]["child_spans"] == []


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
    assert record["child_spans"] == []
    assert handler.get_decisions_as_dicts() == [
        {k: v for k, v in record.items() if k != "child_spans"}
    ]


def test_standalone_synthesize_event_is_exported():
    handler, exporter = _handler()
    handler.on_event_start(EventType.SYNTHESIZE, {"query_str": "why?"}, event_id="s")
    handler.on_event_end(EventType.SYNTHESIZE, {"response": "text answer"}, event_id="s")
    decision = handler.get_decisions()[0]
    assert decision.decision_type == "synthesize"
    assert decision.outputs == {"response": "text answer"}
    assert [r["decision_id"] for r in exporter.records] == ["s"]


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
    # A failed standalone LLM call is exported like a completed one.
    assert [(r["decision_id"], r["error"]) for r in exporter.records] == [("second", "boom")]

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
    # Abandoned roots are exported like any other failed event.
    assert [r["decision_id"] for r in exporter.records] == ["q0", "l1"]
    assert all(r["error"].startswith("abandoned") for r in exporter.records)


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


# Root-level export (standalone events, nesting, failures)

@pytest.mark.parametrize(
    "event_type, start, end, decision_type",
    [
        (EventType.LLM, {"messages": ["hi"]}, {"response": "yo"}, "llm"),
        (EventType.RETRIEVE, {"query_str": "q"}, {"nodes": []}, "retriever"),
        (EventType.EMBEDDING, {"serialized": {}}, {"chunks": ["a"], "embeddings": [[0.1]]},
         "embedding"),
    ],
)
@pytest.mark.parametrize("parent_id", ["", "root", "an-event-this-handler-never-saw"])
def test_standalone_events_are_exported_once(event_type, start, end, decision_type, parent_id):
    # Used to be recorded in memory only: nothing but a query was exported.
    handler, exporter = _handler()
    handler.on_event_start(event_type, start, event_id="e", parent_id=parent_id)
    handler.on_event_end(event_type, end, event_id="e")
    handler.on_event_end(event_type, end, event_id="e")  # a duplicate end is ignored
    assert [(r["decision_id"], r["decision_type"]) for r in exporter.records] == [
        ("e", decision_type)
    ]
    assert exporter.records[0]["child_spans"] == []
    assert handler._root_of == {} and handler._children == {}


def test_nested_events_are_attached_to_their_root_and_not_exported_alone():
    handler, exporter = _handler()
    handler.on_event_start(EventType.QUERY, {"query_str": "why?"}, event_id="q", parent_id="root")
    handler.on_event_start(EventType.RETRIEVE, {"query_str": "why?"}, event_id="r", parent_id="q")
    handler.on_event_start(EventType.EMBEDDING, {}, event_id="e", parent_id="r")
    handler.on_event_end(EventType.EMBEDDING, {"chunks": ["why?"], "embeddings": [[1.0, 2.0]]},
                         event_id="e")
    handler.on_event_end(EventType.RETRIEVE, {"nodes": []}, event_id="r")
    handler.on_event_start(EventType.SYNTHESIZE, {"query_str": "why?"}, event_id="s", parent_id="q")
    handler.on_event_start(EventType.LLM, {"messages": ["why?"]}, event_id="l", parent_id="s")
    handler.on_event_end(EventType.LLM, {"response": "because"}, event_id="l")
    handler.on_event_end(EventType.SYNTHESIZE, {"response": "because"}, event_id="s")
    assert exporter.records == []  # nothing leaves before the root ends

    handler.on_event_end(EventType.QUERY, {"response": "because"}, event_id="q")
    assert len(exporter.records) == 1
    record = exporter.records[0]
    assert record["decision_id"] == "q"
    assert [(c["decision_id"], c["parent_run_id"]) for c in record["child_spans"]] == [
        ("e", "r"), ("r", "q"), ("l", "s"), ("s", "q"),
    ]
    assert handler.decision_count == 5
    assert handler._root_of == {} and handler._children == {}


def test_events_under_an_untracked_parent_are_their_own_roots():
    # With queries filtered out, the LLM call inside one has no tracked
    # ancestor; it must still be exported.
    handler, exporter = _handler(capture_queries=False)
    handler.on_event_start(EventType.QUERY, {"query_str": "q"}, event_id="q", parent_id="root")
    handler.on_event_start(EventType.SYNTHESIZE, {"query_str": "q"}, event_id="s", parent_id="q")
    handler.on_event_start(EventType.LLM, {"messages": ["q"]}, event_id="l", parent_id="s")
    handler.on_event_end(EventType.LLM, {"response": "a"}, event_id="l")
    handler.on_event_end(EventType.SYNTHESIZE, {"response": "a"}, event_id="s")
    handler.on_event_end(EventType.QUERY, {"response": "a"}, event_id="q")
    assert [r["decision_id"] for r in exporter.records] == ["l"]


def test_end_event_carrying_an_exception_marks_the_decision_failed():
    # LlamaIndex reports a failure as the event's own end with an
    # "exception" payload, not as a separate exception event.
    handler, exporter = _handler()
    handler.on_event_start(EventType.LLM, {"messages": ["hi"]}, event_id="l", parent_id="root")
    handler.on_event_end(EventType.LLM, {"exception": TimeoutError("provider timed out")},
                         event_id="l")
    handler.on_event_start(EventType.QUERY, {"query_str": "q"}, event_id="q", parent_id="root")
    handler.on_event_start(EventType.RETRIEVE, {"query_str": "q"}, event_id="r", parent_id="q")
    handler.on_event_end(EventType.RETRIEVE, {"exception": RuntimeError("")}, event_id="r")
    handler.on_event_end(EventType.QUERY, {"exception": RuntimeError("index offline")},
                         event_id="q")

    llm, query = exporter.records
    assert (llm["decision_id"], llm["error"], llm["outputs"]) == ("l", "provider timed out", {})
    assert (query["decision_id"], query["error"]) == ("q", "index offline")
    # An exception with an empty message falls back to its class name.
    assert [(c["decision_id"], c["error"]) for c in query["child_spans"]] == [("r", "RuntimeError")]


def test_child_of_an_abandoned_root_is_exported_on_its_own():
    handler, exporter = _handler(max_inflight=2)
    handler.on_event_start(EventType.QUERY, {"query_str": "q"}, event_id="q", parent_id="root")
    handler.on_event_start(EventType.LLM, {}, event_id="l1", parent_id="q")
    handler.on_event_start(EventType.LLM, {}, event_id="l2", parent_id="q")  # evicts q
    assert [r["decision_id"] for r in exporter.records] == ["q"]
    handler.on_event_end(EventType.LLM, {"response": "late"}, event_id="l1")
    assert [r["decision_id"] for r in exporter.records] == ["q", "l1"]


# Payload shapes of current LlamaIndex (verified against llama-index-core 0.14.26)

def test_llm_start_reads_model_and_prompt_from_current_payload_keys():
    handler, _ = _handler()
    handler.on_event_start(
        EventType.LLM,
        {
            "formatted_prompt": "Say hi",
            "additional_kwargs": {},
            "serialized": {"class_name": "OpenAI", "model": "gpt-4o-mini",
                           "temperature": 0.1, "max_tokens": None},
        },
        event_id="l",
    )
    handler.on_event_end(EventType.LLM, {"formatted_prompt": "Say hi",
                                         "completion": SimpleNamespace(text="hi")}, event_id="l")
    decision = handler.get_decisions()[0]
    assert decision.function_name == "gpt-4o-mini"
    assert decision.inputs == {"prompt": "Say hi"}
    assert decision.outputs == {"text": "hi"}
    assert decision.model_parameters == {"temperature": 0.1}

    handler.on_event_start(EventType.LLM, {"serialized": {"class_name": "MockLLM"}}, event_id="m")
    handler.on_event_end(
        EventType.LLM,
        {"response": SimpleNamespace(message=SimpleNamespace(content=None))},
        event_id="m",
    )
    fallback = handler.get_decisions()[1]
    assert fallback.function_name == "MockLLM"
    assert fallback.outputs == {"text": ""}  # a tool-call-only message has no content


@pytest.mark.parametrize(
    "response, expected",
    [
        # OpenAI: raw is the SDK response object, usage an object on it.
        (SimpleNamespace(text="x", raw=SimpleNamespace(usage=SimpleNamespace(
            prompt_tokens=11, completion_tokens=4, total_tokens=15)), additional_kwargs={}),
         (11, 4, 15)),
        # Anthropic-style dict raw with input/output spellings and no total.
        (SimpleNamespace(text="x", raw={"usage": {"input_tokens": 7, "output_tokens": 2}},
                         additional_kwargs={}),
         (7, 2, 9)),
        # Gemini: usage_metadata with *_token_count names.
        (SimpleNamespace(text="x", raw={"usage_metadata": {
            "prompt_token_count": 5, "candidates_token_count": 6, "total_token_count": 11}},
            additional_kwargs={}),
         (5, 6, 11)),
        # Integrations that flatten usage into additional_kwargs.
        (SimpleNamespace(text="x", raw=None,
                         additional_kwargs={"prompt_tokens": 3, "completion_tokens": 1,
                                            "total_tokens": 4}),
         (3, 1, 4)),
        # raw without usage falls through to additional_kwargs.
        (SimpleNamespace(text="x", raw={"id": "r1"},
                         additional_kwargs={"input_tokens": 2, "output_tokens": 2}),
         (2, 2, 4)),
    ],
)
@pytest.mark.parametrize("key", ["response", "completion"])
def test_token_usage_is_read_from_the_response(response, expected, key):
    handler, exporter = _handler()
    handler.on_event_start(EventType.LLM, {}, event_id="l")
    handler.on_event_end(EventType.LLM, {key: response}, event_id="l")
    prompt, completion, total = expected
    usage = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}
    assert handler.get_decisions()[0].token_usage == usage
    assert exporter.records[0]["token_usage"] == usage


@pytest.mark.parametrize(
    "response",
    [
        "plain string",
        SimpleNamespace(text="x", raw=None, additional_kwargs={}),
        SimpleNamespace(text="x", raw={"usage": None}, additional_kwargs={}),
        SimpleNamespace(text="x", raw={"usage": {"prompt_tokens": True}}, additional_kwargs={}),
        SimpleNamespace(text="x", raw={"usage": {"service_tier": "default"}},
                        additional_kwargs={"finish_reason": "stop"}),
    ],
)
def test_token_usage_is_absent_when_the_provider_reported_none(response):
    # Never estimated: an unreported count stays unreported.
    handler, _ = _handler()
    handler.on_event_start(EventType.LLM, {}, event_id="l")
    handler.on_event_end(EventType.LLM, {"response": response}, event_id="l")
    assert handler.get_decisions()[0].token_usage is None


def test_embedding_end_reads_texts_from_chunks_and_vectors_from_embeddings():
    handler, _ = _handler()
    handler.on_event_start(
        EventType.EMBEDDING,
        {"serialized": {"class_name": "OpenAIEmbedding", "model_name": "text-embedding-3-small"}},
        event_id="e",
    )
    handler.on_event_end(
        EventType.EMBEDDING,
        {"chunks": ["one", "two", "three"], "embeddings": [[0.1, 0.2]] * 3},
        event_id="e",
    )
    decision = handler.get_decisions()[0]
    assert decision.function_name == "text-embedding-3-small"
    assert decision.inputs == {"text_count": 3}
    assert decision.outputs == {"embedding_count": 3, "dimensions": 2}


def test_placeholder_model_name_yields_to_the_class_name_and_roles_use_enum_values():
    import enum

    class Role(str, enum.Enum):  # same shape as llama_index MessageRole
        USER = "user"

    handler, _ = _handler()
    handler.on_event_start(
        EventType.LLM,
        {"messages": [SimpleNamespace(role=Role.USER, content="hi")],
         "serialized": {"class_name": "MockLLM", "model_name": "unknown"}},
        event_id="l",
    )
    handler.on_event_end(EventType.LLM, {"response": "ok"}, event_id="l")
    handler.on_event_start(
        EventType.EMBEDDING,
        {"serialized": {"class_name": "MockEmbedding", "model_name": "unknown"}}, event_id="e",
    )
    handler.on_event_end(EventType.EMBEDDING, {"chunks": ["a"], "embeddings": [[1.0]]},
                         event_id="e")
    llm, embedding = handler.get_decisions()
    assert llm.function_name == "MockLLM"
    assert llm.inputs == {"messages": [{"role": "user", "content": "hi"}]}
    assert embedding.function_name == "MockEmbedding"
