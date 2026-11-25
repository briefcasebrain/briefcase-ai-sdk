"""Shared handler state: content options, record ring buffer, in-flight caps.

Also checks that every framework handler exposes the same options.
"""

import inspect

import pytest

from briefcase.config import BriefcaseConfig
from briefcase.decorators import CAPTURE_CONTENT_MODES as DECORATOR_MODES
from briefcase.integrations.frameworks._capture_state import (
    CAPTURE_CONTENT_MODES,
    CaptureOptions,
    InflightStore,
    bounded_records,
)
from briefcase.integrations.frameworks.ag2_handler import AG2HookTracer
from briefcase.integrations.frameworks.autogen_handler import AutoGenEventHandler
from briefcase.integrations.frameworks.crewai_handler import CrewAIEventListener
from briefcase.integrations.frameworks.langchain_handler import BriefcaseLangChainHandler
from briefcase.integrations.frameworks.llamaindex_handler import BriefcaseLlamaIndexHandler
from briefcase.integrations.frameworks.openai_agents_handler import OpenAIAgentsTracer
from briefcase.integrations.frameworks.pageindex_handler import PageIndexTracer

ALL_HANDLERS = [
    AG2HookTracer,
    AutoGenEventHandler,
    CrewAIEventListener,
    BriefcaseLangChainHandler,
    BriefcaseLlamaIndexHandler,
    OpenAIAgentsTracer,
    PageIndexTracer,
]
INFLIGHT_HANDLERS = [
    CrewAIEventListener,
    BriefcaseLangChainHandler,
    BriefcaseLlamaIndexHandler,
    OpenAIAgentsTracer,
]


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


# CaptureOptions

def test_modes_match_capture_decorator():
    assert CAPTURE_CONTENT_MODES == DECORATOR_MODES
    with pytest.raises(ValueError):
        CaptureOptions("everything")
    with pytest.raises(TypeError):
        CaptureOptions(redact="nope")


def test_default_records_no_content():
    record = {"inputs": {"q": "secret"}, "outputs": {"a": "secret"}, "error": "secret"}
    assert CaptureOptions().apply(record) == {
        "inputs": {}, "outputs": {}, "error": "<omitted>",
    }


def test_bool_values_match_the_providers():
    assert CaptureOptions(True).capture_content == "full"
    assert CaptureOptions(False).capture_content == "none"
    assert CaptureOptions(None).capture_content == "none"


def test_passthrough_leaves_record_untouched():
    record = {"inputs": {"q": "secret"}, "outputs": {"a": 1}, "error": "boom"}
    assert CaptureOptions("full").apply(record) == {
        "inputs": {"q": "secret"}, "outputs": {"a": 1}, "error": "boom",
    }


def test_redact_reaches_nested_strings_and_spans():
    options = CaptureOptions("full", redact=lambda text: text.replace("secret", "[X]"))
    record = {
        "function_name": "secret-fn",
        "inputs": {"messages": [{"role": "user", "content": "my secret"}], "n": 3},
        "outputs": {"text": "a secret", "ok": True, "none": None},
        "error": "secret failure",
        "child_spans": [{"inputs": {"input": "secret"}, "outputs": {}}],
        "spans": [{"input": "secret in", "output": ["secret out"], "type": "tool_call"}],
    }
    options.apply(record)
    assert record["inputs"] == {"messages": [{"role": "user", "content": "my [X]"}], "n": 3}
    assert record["outputs"] == {"text": "a [X]", "ok": True, "none": None}
    assert record["error"] == "[X] failure"
    assert record["child_spans"][0]["inputs"] == {"input": "[X]"}
    assert record["spans"][0]["input"] == "[X] in"
    assert record["spans"][0]["output"] == ["[X] out"]
    # Metadata is not content.
    assert record["function_name"] == "secret-fn"


def test_redact_fails_closed():
    def broken(text):
        raise RuntimeError("redactor down")

    record = CaptureOptions("full", redact=broken).apply(
        {"inputs": {"q": "secret"}, "outputs": {}, "error": "secret"}
    )
    assert record["inputs"] == {"q": "<redaction-failed>"}
    assert record["error"] == "<redaction-failed>"
    assert CaptureOptions("full", redact=lambda t: None).content("secret") == "<redaction-failed>"


def test_hash_and_none_modes_drop_content():
    hashed = CaptureOptions("hash").apply(
        {"inputs": {"q": "secret"}, "outputs": {}, "error": "secret"}
    )
    assert set(hashed["inputs"]) == {"sha256", "chars"}
    assert len(hashed["inputs"]["sha256"]) == 64
    assert hashed["outputs"] == {}
    assert hashed["error"].startswith("sha256:")
    assert "secret" not in repr(hashed)

    none = CaptureOptions("none").apply(
        {"inputs": {"q": "secret"}, "outputs": {"a": "secret"}, "error": "secret"}
    )
    assert none == {"inputs": {}, "outputs": {}, "error": "<omitted>"}


# Record ring buffer

def test_bounded_records_is_a_ring_buffer_that_scrubs():
    buffer = bounded_records(3, CaptureOptions("none"))
    for i in range(10):
        buffer.append({"decision_id": i, "inputs": {"q": "secret"}})
    assert [r["decision_id"] for r in buffer] == [7, 8, 9]
    assert all(r["inputs"] == {} for r in buffer)

    assert len(bounded_records(None)) == 0
    export_only = bounded_records(0, CaptureOptions("none"))
    record = {"inputs": {"q": "secret"}}
    export_only.append(record)
    assert len(export_only) == 0 and record["inputs"] == {}
    with pytest.raises(ValueError):
        bounded_records(-1)


# In-flight store

def test_inflight_size_cap_evicts_oldest():
    evicted = []
    store = InflightStore(
        max_size=2, ttl_seconds=None,
        on_evict=lambda key, value, reason: evicted.append((key, value, reason)),
    )
    store["a"] = 1
    store["b"] = 2
    store["c"] = 3
    assert evicted == [("a", 1, "size")]
    assert store == {"b": 2, "c": 3}
    assert store.pop("b") == 2 and store.pop("missing", "d") == "d"
    assert "c" in store and len(store) == 1 and list(store) == ["c"]
    assert store.evicted == 1


def test_inflight_ttl_expires_entries():
    now = [0.0]
    evicted = []
    store = InflightStore(
        max_size=None, ttl_seconds=10,
        on_evict=lambda key, value, reason: evicted.append((key, reason)),
        clock=lambda: now[0],
    )
    store["old"] = 1
    now[0] = 5.0
    store["mid"] = 2
    now[0] = 12.0
    store["new"] = 3
    assert evicted == [("old", "ttl")]
    assert store.keys() == ["mid", "new"]

    with pytest.raises(ValueError):
        InflightStore(max_size=0)
    with pytest.raises(ValueError):
        InflightStore(ttl_seconds=0)


def test_inflight_evict_callback_errors_are_contained():
    def boom(key, value, reason):
        raise RuntimeError("callback failed")

    store = InflightStore(max_size=1, on_evict=boom)
    store["a"] = 1
    store["b"] = 2
    assert store == {"b": 2}


# Every handler exposes the same options

@pytest.mark.parametrize("handler_cls", ALL_HANDLERS)
def test_handlers_accept_capture_and_bound_options(handler_cls):
    params = inspect.signature(handler_cls.__init__).parameters
    for name in ("capture_content", "redact", "max_records"):
        assert name in params, f"{handler_cls.__name__} lacks {name}"
    assert params["capture_content"].default == "none"
    assert params["redact"].default is None

    with pytest.raises(ValueError):
        handler_cls(capture_content="bogus")

    handler = handler_cls(max_records=2, capture_content="none")
    store = handler._decisions if hasattr(handler, "_decisions") else handler._records
    assert store.maxlen == 2


@pytest.mark.parametrize("handler_cls", INFLIGHT_HANDLERS)
def test_handlers_with_inflight_state_accept_caps(handler_cls):
    params = inspect.signature(handler_cls.__init__).parameters
    assert "max_inflight" in params and "inflight_ttl_seconds" in params
    handler = handler_cls(max_inflight=5, inflight_ttl_seconds=30)
    store = handler._traces if hasattr(handler, "_traces") else handler._inflight
    assert (store.max_size, store.ttl_seconds) == (5, 30)


# Behaviour through real handlers

def test_ag2_records_bounded_and_redacted():
    exporter = _Exporter()
    tracer = AG2HookTracer(
        exporter=exporter, async_capture=False, max_records=3, capture_content="full",
        redact=lambda text: text.replace("card-number", "****"),
    )
    for i in range(10):
        record = tracer._build_record("message", inputs={"content": f"card card-number #{i}"})
        tracer._append_and_export(record)

    assert tracer.decision_count == 3
    assert [r["inputs"]["content"] for r in tracer.get_records()] == [
        "card **** #7", "card **** #8", "card **** #9",
    ]
    # Every record was still exported, and exported redacted.
    assert len(exporter.records) == 10
    assert all("card-number" not in repr(r) for r in exporter.records)


def test_crewai_abandoned_start_is_recorded_not_leaked():
    exporter = _Exporter()
    listener = CrewAIEventListener(
        exporter=exporter, async_capture=False, max_inflight=2,
        capture_content="none",
    )

    class Event:
        def __init__(self, key):
            self.tool_name = key
            self.query = "secret query"

    for key in ("a", "b", "c"):
        listener._generic_start("tool", "tool_usage", Event(key), "tool_name")

    assert len(listener._inflight) == 2
    abandoned = listener.get_records()
    assert len(abandoned) == 1
    assert abandoned[0]["function_name"] == "a"
    assert abandoned[0]["error"] == "<omitted>"
    assert abandoned[0]["inputs"] == {}
    assert exporter.records == abandoned


def test_openai_agents_abandoned_trace_and_span_caps():
    tracer = OpenAIAgentsTracer(async_capture=False, max_inflight=2, exporter=_Exporter(),
                                capture_content="full")

    class Trace:
        def __init__(self, trace_id):
            self.trace_id = trace_id
            self.name = f"trace-{trace_id}"

    class Span:
        def __init__(self, span_id):
            self.span_id = span_id

    for i in range(5):
        tracer.on_trace_start(Trace(f"t{i}"))
        tracer.on_span_start(Span(f"s{i}"))

    assert len(tracer._traces) == 2
    assert len(tracer._span_starts) == 2
    abandoned = tracer.get_records()
    assert [r["trace_id"] for r in abandoned] == ["t0", "t1", "t2"]
    assert all(r["error"].startswith("abandoned") for r in abandoned)
