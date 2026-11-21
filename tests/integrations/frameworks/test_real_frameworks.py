"""Auto-instrumentation smoke against real framework packages.

Runs only with BRIEFCASE_REAL_FRAMEWORKS=1 (the CI lane installs the
framework extras and invokes this file directly); each framework section
additionally skips when its package is absent. The stub-driven suites cover
handler behavior; this file proves the patch points exist in the real
packages: auto() installs against the actual module structure and undo()
restores it.
"""

from __future__ import annotations

import os

import pytest

from briefcase.auto import _PATCHES, auto, undo
from briefcase.exporters import MemoryExporter

pytestmark = pytest.mark.skipif(
    os.environ.get("BRIEFCASE_REAL_FRAMEWORKS") != "1",
    reason="real-frameworks lane only (BRIEFCASE_REAL_FRAMEWORKS=1)",
)


@pytest.fixture(autouse=True)
def _undo_all():
    yield
    undo()


def test_langchain_patch_and_capture_roundtrip():
    pytest.importorskip("langchain_core")
    from langchain_core.runnables import RunnableLambda

    exporter = MemoryExporter()
    auto("langchain", exporter=exporter, async_capture=False)
    assert "langchain" in _PATCHES
    assert RunnableLambda(lambda x: x + 1).invoke(1) == 2
    undo("langchain")
    assert "langchain" not in _PATCHES
    assert RunnableLambda(lambda x: x + 1).invoke(1) == 2


def test_llamaindex_patch_installs_into_settings():
    pytest.importorskip("llama_index.core")
    from llama_index.core import Settings

    auto("llamaindex", exporter=MemoryExporter(), async_capture=False)
    assert "llamaindex" in _PATCHES
    handlers = list(getattr(Settings.callback_manager, "handlers", []))
    assert any("Briefcase" in type(h).__name__ for h in handlers)
    undo("llamaindex")
    assert "llamaindex" not in _PATCHES


class _LlamaIndexRecords:
    def __init__(self):
        self.records = []

    async def export(self, record):
        self.records.append(record)


def _llamaindex_handler():
    from briefcase.integrations.frameworks.llamaindex_handler import BriefcaseLlamaIndexHandler

    exporter = _LlamaIndexRecords()
    return BriefcaseLlamaIndexHandler(exporter=exporter, async_capture=False), exporter


def test_llamaindex_standalone_events_are_exported():
    """Last verified against llama-index-core 0.14.26."""
    pytest.importorskip("llama_index.core")
    from llama_index.core import MockEmbedding
    from llama_index.core.callbacks import CallbackManager
    from llama_index.core.llms import ChatMessage, MockLLM

    handler, exporter = _llamaindex_handler()
    manager = CallbackManager([handler])
    llm = MockLLM(max_tokens=3, callback_manager=manager)
    llm.complete("hello world")
    llm.chat([ChatMessage(role="user", content="hi")])
    MockEmbedding(embed_dim=4, callback_manager=manager).get_text_embedding("abc")

    kinds = [(r["decision_type"], r["function_name"]) for r in exporter.records]
    assert ("embedding", "MockEmbedding") in kinds
    assert kinds.count(("llm", "MockLLM")) >= 2
    completion = exporter.records[0]
    assert completion["inputs"] == {"prompt": "hello world"}
    assert completion["outputs"]["text"]
    chat = [r for r in exporter.records if "messages" in r["inputs"]][0]
    assert chat["inputs"]["messages"] == [{"role": "user", "content": "hi"}]
    embedding = [r for r in exporter.records if r["decision_type"] == "embedding"][0]
    assert embedding["inputs"] == {"text_count": 1}
    assert embedding["outputs"] == {"embedding_count": 1, "dimensions": 4}


def test_llamaindex_query_is_exported_once_with_its_nested_events():
    pytest.importorskip("llama_index.core")
    from llama_index.core import Document, MockEmbedding, Settings, VectorStoreIndex
    from llama_index.core.callbacks import CallbackManager
    from llama_index.core.llms import MockLLM

    handler, exporter = _llamaindex_handler()
    saved = (Settings.callback_manager, Settings._llm, Settings._embed_model)
    try:
        Settings.callback_manager = CallbackManager([handler])
        Settings.llm = MockLLM(max_tokens=3)
        Settings.embed_model = MockEmbedding(embed_dim=4)
        index = VectorStoreIndex.from_documents([Document(text="the sky is blue")])
        exporter.records.clear()
        index.as_query_engine().query("what color?")
    finally:
        Settings.callback_manager, Settings._llm, Settings._embed_model = saved

    assert [r["decision_type"] for r in exporter.records] == ["query"]
    children = [c["decision_type"] for c in exporter.records[0]["child_spans"]]
    assert sorted(children) == ["embedding", "llm", "retriever", "synthesize"]


def test_llamaindex_failure_arrives_as_an_end_event_and_is_exported():
    pytest.importorskip("llama_index.core")
    from llama_index.core.callbacks import CallbackManager
    from llama_index.core.llms import MockLLM
    from llama_index.core.llms.callbacks import llm_completion_callback

    class FailingLLM(MockLLM):
        @llm_completion_callback()
        def complete(self, prompt, formatted=False, **kwargs):
            raise RuntimeError("provider down")

    handler, exporter = _llamaindex_handler()
    with pytest.raises(RuntimeError):
        FailingLLM(callback_manager=CallbackManager([handler])).complete("hi")
    assert [(r["decision_type"], r["error"]) for r in exporter.records] == [
        ("llm", "provider down")
    ]


def test_llamaindex_token_usage_is_read_from_the_response_raw():
    pytest.importorskip("llama_index.core")
    from llama_index.core.callbacks import CallbackManager
    from llama_index.core.llms import CompletionResponse, MockLLM
    from llama_index.core.llms.callbacks import llm_completion_callback

    class UsageLLM(MockLLM):
        @llm_completion_callback()
        def complete(self, prompt, formatted=False, **kwargs):
            return CompletionResponse(
                text="ok", raw={"usage": {"prompt_tokens": 9, "completion_tokens": 2}}
            )

    handler, exporter = _llamaindex_handler()
    UsageLLM(callback_manager=CallbackManager([handler])).complete("hi")
    assert exporter.records[0]["token_usage"] == {
        "prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11,
    }


def test_crewai_event_imports_resolve():
    pytest.importorskip("crewai")
    # The listener imports event classes from crewai's event tree; this
    # proves those import paths exist in the installed release.
    auto("crewai", exporter=MemoryExporter(), async_capture=False)
    assert "crewai" in _PATCHES
    undo("crewai")


def test_ag2_agent_init_hook_installs():
    pytest.importorskip("autogen")
    auto("ag2", exporter=MemoryExporter(), async_capture=False)
    assert "ag2" in _PATCHES
    undo("ag2")


def test_autogen_agentchat_handler_installs():
    pytest.importorskip("autogen_agentchat")
    auto("autogen", exporter=MemoryExporter(), async_capture=False)
    assert "autogen" in _PATCHES
    undo("autogen")


def test_openai_agents_processor_installs():
    pytest.importorskip("agents")
    auto("openai-agents", exporter=MemoryExporter(), async_capture=False)
    assert "openai-agents" in _PATCHES
    undo("openai-agents")
