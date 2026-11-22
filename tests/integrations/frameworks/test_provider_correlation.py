"""The framework handlers publish their LLM runs so a provider record made
beneath one is linked to it (``parent_run_id``), and honour
``briefcase.auto(dedupe="framework")``."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import briefcase
from briefcase.integrations.frameworks._capture_state import LLMRunScopes
from briefcase.integrations.frameworks.langchain_handler import BriefcaseLangChainHandler
from briefcase.integrations.frameworks.llamaindex_handler import (
    BriefcaseLlamaIndexHandler,
    EventType,
)
from briefcase.integrations.providers._base import ProviderInstrumentor
from briefcase.integrations.providers._correlation import current_llm_run, set_dedupe


@pytest.fixture(autouse=True)
def _reset():
    from briefcase.integrations.providers._correlation import reset_context

    set_dedupe("none")
    reset_context()
    yield
    set_dedupe("none")
    reset_context()


@pytest.fixture
def mem():
    return briefcase.MemoryExporter()


@pytest.fixture
def provider(mem):
    inst = ProviderInstrumentor("openai", exporter=mem, async_capture=False)
    yield lambda: inst.begin("llm.openai.chat", "openai.chat.completions.create").finish(
        {"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    inst.uninstall()


def _response(text="hi"):
    response = MagicMock()
    response.generations = [[MagicMock(text=text)]]
    response.llm_output = None
    return response


def test_langchain_declares_every_flag_langchain_core_reads():
    handler = BriefcaseLangChainHandler(capture_llm=False)
    # langchain-core 1.x reads these on every event; a missing one raised
    # AttributeError inside LangChain and the chat-model run was lost.
    for flag in ("ignore_llm", "ignore_chat_model", "ignore_chain", "ignore_agent",
                 "ignore_retriever", "ignore_retry", "ignore_custom_event",
                 "raise_error", "run_inline"):
        assert isinstance(getattr(handler, flag), bool), flag
    assert handler.ignore_chat_model is True and handler.run_inline is True


def test_langchain_llm_run_links_the_provider_record(mem, provider):
    handler = BriefcaseLangChainHandler(exporter=mem, async_capture=False)
    handler.on_chain_start({"name": "chain"}, {"q": 1}, run_id="chain-1")
    assert current_llm_run() is None  # a chain is not a model call
    provider()
    handler.on_llm_start({"name": "ChatOpenAI"}, ["prompt"], run_id="llm-1", parent_run_id="chain-1")
    assert current_llm_run().run_id == "llm-1"
    provider()
    handler.on_llm_end(_response(), run_id="llm-1")
    assert current_llm_run() is None
    provider()
    handler.on_chain_end({"out": 1}, run_id="chain-1")

    provider_records = [r for r in mem.records if r["decision_type"] == "llm.openai.chat"]
    assert [r.get("parent_run_id") for r in provider_records] == [None, "llm-1", None]
    assert provider_records[1]["parent_framework"] == "langchain"
    chain = [r for r in mem.records if r["decision_type"] == "chain"][0]
    assert [c["decision_id"] for c in chain["child_spans"]] == ["llm-1"]


def test_langchain_error_and_eviction_end_the_run(mem, provider):
    handler = BriefcaseLangChainHandler(exporter=mem, async_capture=False, max_inflight=1)
    handler.on_llm_start({}, ["p"], run_id="a")
    handler.on_llm_error(RuntimeError("x"), run_id="a")
    assert current_llm_run() is None
    handler.on_llm_start({}, ["p"], run_id="b")
    handler.on_llm_start({}, ["p"], run_id="c")  # evicts b
    handler.on_llm_end(_response(), run_id="c")
    assert current_llm_run() is None


def test_langchain_dedupe_framework_drops_the_llm_run_but_keeps_the_chain(mem, provider):
    set_dedupe("framework")
    handler = BriefcaseLangChainHandler(exporter=mem, async_capture=False)
    handler.on_chain_start({"name": "chain"}, {"q": 1}, run_id="chain-1")
    handler.on_llm_start({}, ["p"], run_id="llm-1", parent_run_id="chain-1")
    provider()
    handler.on_llm_end(_response(), run_id="llm-1")
    # a model call briefcase has no provider record for keeps its LLM run
    handler.on_llm_start({}, ["p"], run_id="llm-2", parent_run_id="chain-1")
    handler.on_llm_end(_response(), run_id="llm-2")
    handler.on_chain_end({"out": 1}, run_id="chain-1")

    chain = [r for r in mem.records if r["decision_type"] == "chain"][0]
    assert [c["decision_id"] for c in chain["child_spans"]] == ["llm-2"]
    assert [d.decision_id for d in handler.get_decisions()] == ["llm-2", "chain-1"]
    assert sum(r["decision_type"] == "llm.openai.chat" for r in mem.records) == 1


def test_llamaindex_llm_event_links_and_dedupes(mem, provider):
    handler = BriefcaseLlamaIndexHandler(exporter=mem, async_capture=False)
    handler.on_event_start(EventType.LLM, {"model_dict": {"model": "m"}}, event_id="e1")
    assert current_llm_run().framework == "llamaindex"
    provider()
    handler.on_event_end(EventType.LLM, {}, event_id="e1")
    assert current_llm_run() is None
    assert [r.get("parent_run_id") for r in mem.records if "openai" in r["decision_type"]] == ["e1"]
    assert sum(r["decision_type"] == "llm" for r in mem.records) == 1

    mem.records.clear()
    set_dedupe("framework")
    handler.on_event_start(EventType.LLM, {"model_dict": {"model": "m"}}, event_id="e2")
    provider()
    handler.on_event_end(EventType.LLM, {}, event_id="e2")
    assert [r["decision_type"] for r in mem.records] == ["llm.openai.chat"]


def test_crewai_llm_events_link_and_dedupe(mem, provider):
    from tests.integrations.frameworks import test_crewai_handler as crew

    bus = crew._make_mock_bus()
    crew._make_listener_with_mock_bus(bus, exporter=mem, async_capture=False)
    start = crew._make_crew_event("LLMCallStartedEvent", model="gpt", call_id="c1")
    done = crew._make_crew_event("LLMCallCompletedEvent", call_id="c1", output="x")
    crew._fire_on_bus(bus, "LLMCallStartedEvent", start)
    assert current_llm_run().framework == "crewai"
    provider()
    crew._fire_on_bus(bus, "LLMCallCompletedEvent", done)
    assert current_llm_run() is None
    assert sorted(r["decision_type"] for r in mem.records) == ["llm.openai.chat", "llm_call"]
    assert [r["parent_framework"] for r in mem.records if "parent_run_id" in r] == ["crewai"]

    mem.records.clear()
    set_dedupe("framework")
    crew._fire_on_bus(bus, "LLMCallStartedEvent", start)
    provider()
    crew._fire_on_bus(bus, "LLMCallCompletedEvent", done)
    assert [r["decision_type"] for r in mem.records] == ["llm.openai.chat"]


def test_openai_agents_model_spans_link_and_dedupe(mem, provider):
    from briefcase.integrations.frameworks.openai_agents_handler import OpenAIAgentsTracer

    tracer = OpenAIAgentsTracer(exporter=mem, async_capture=False)

    def span(span_id, kind):
        return SimpleNamespace(span_id=span_id, trace_id="t1", parent_id=None, error=None,
                               span_data=SimpleNamespace(type=kind, name=kind))

    trace = SimpleNamespace(trace_id="t1", name="run", metadata=None, group_id=None)
    tracer.on_trace_start(trace)
    tool = span("s-tool", "function")
    tracer.on_span_start(tool)
    assert current_llm_run() is None
    tracer.on_span_end(tool)
    for dedupe, kind in (("none", "generation"), ("framework", "response")):
        set_dedupe(dedupe)
        model = span(f"s-{kind}", kind)
        tracer.on_span_start(model)
        assert current_llm_run().framework == "openai-agents"
        provider()
        tracer.on_span_end(model)
        assert current_llm_run() is None
    tracer.on_trace_end(trace)

    links = [r["parent_run_id"] for r in mem.records if r["decision_type"] == "llm.openai.chat"]
    assert links == ["s-generation", "s-response"]
    trace_record = [r for r in mem.records if r["decision_type"] != "llm.openai.chat"][0]
    # the tool span and the generation span are kept; the response span was
    # dropped in favour of its provider record (dedupe="framework")
    assert len(trace_record["spans"]) == 2


def test_scopes_are_bounded_and_cannot_raise():
    scopes = LLMRunScopes("langchain", max_size=3)
    for i in range(10):
        scopes.start(f"run-{i}")
    assert len(scopes._scopes) == 3
    assert scopes.finish("run-0") is False  # forgotten, and switched off
    assert scopes.finish("never-started") is False
    scopes.start(object())  # unhashable-ish keys and odd values do not raise
    scopes.start([1, 2])
    scopes.clear()
    assert current_llm_run() is None


@pytest.fixture
def real_llama_index():
    """The installed llama-index-core, not the stand-in other handler tests
    put in ``sys.modules``; the stand-in is put back afterwards."""
    import importlib
    import importlib.util
    import sys

    saved = {k: v for k, v in sys.modules.items() if k == "llama_index" or k.startswith("llama_index.")}
    for key in saved:
        del sys.modules[key]
    try:
        if importlib.util.find_spec("llama_index.core") is None:
            pytest.skip("llama-index-core is not installed")
        yield importlib.import_module("llama_index.core.callbacks")
    finally:
        for key in [k for k in sys.modules if k == "llama_index" or k.startswith("llama_index.")]:
            del sys.modules[key]
        sys.modules.update(saved)


def test_real_llamaindex_callback_manager_over_the_real_openai_sdk(mem, real_llama_index):
    """Not a stand-in: llama-index-core's own CallbackManager dispatches the
    LLM event to the handler, and the openai SDK (instrumented by
    auto("openai"), HTTP answered by a mock transport) makes the call inside
    it, in a plain call, in concurrent asyncio tasks and in threads."""
    import asyncio
    import threading

    pytest.importorskip("openai")
    CallbackManager, CBEventType = real_llama_index.CallbackManager, real_llama_index.CBEventType

    from briefcase.auto import auto, undo
    from tests.providers import test_openai as T

    undo()
    try:
        auto("openai", exporter=mem, async_capture=False)
        handler = BriefcaseLlamaIndexHandler(exporter=mem, async_capture=False)
        manager = CallbackManager([handler])

        def one(client):
            with manager.event(CBEventType.LLM, payload={"model_dict": {"model": T.MODEL}}) as event:
                client.chat.completions.create(model=T.MODEL, messages=T.MESSAGES)
                event.on_end(payload={})
            return event._event_id

        async def one_async():
            client = T._async_client()
            with manager.event(CBEventType.LLM, payload={"model_dict": {"model": T.MODEL}}) as event:
                await client.chat.completions.create(model=T.MODEL, messages=T.MESSAGES)
                event.on_end(payload={})
            return event._event_id

        async def many():
            return await asyncio.gather(*[one_async() for _ in range(4)])

        ids = [one(T._client())]
        ids += asyncio.run(many())
        threads = [threading.Thread(target=lambda: ids.append(one(T._client()))) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        T._client().chat.completions.create(model=T.MODEL, messages=T.MESSAGES)  # outside any run

        provider = [r for r in mem.records if r["decision_type"] == "llm.openai.chat"]
        framework = [r for r in mem.records if r["decision_type"] == "llm"]
        assert len(ids) == 9 and len(framework) == 9 and len(provider) == 10
        linked = sorted(r["parent_run_id"] for r in provider if r.get("parent_run_id"))
        assert linked == sorted(ids)  # each call linked to its own run, none crossed
        assert all(r["parent_framework"] == "llamaindex" for r in provider if r.get("parent_run_id"))
        assert sum("parent_run_id" not in r for r in provider) == 1
    finally:
        undo()
