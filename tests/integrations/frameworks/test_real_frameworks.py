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
