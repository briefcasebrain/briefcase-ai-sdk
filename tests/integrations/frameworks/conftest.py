"""Fixtures for the framework integration tests.

Installs stub modules in ``sys.modules`` for every framework the handlers
under ``briefcase/integrations/frameworks/`` observe (langchain_core,
llama_index, crewai, the ag2 ``autogen`` namespace, autogen_agentchat, the
openai-agents ``agents`` package, and pageindex) before any test module
imports the handlers. None of these frameworks is installed in CI; the
handlers import them behind availability flags, so the stubs satisfy those
imports and the tests drive the handlers through duck-typed fake objects.
"""

from __future__ import annotations

import sys
from importlib.machinery import ModuleSpec
from types import ModuleType
from unittest.mock import MagicMock


def _new_module(name: str) -> ModuleType:
    mod = ModuleType(name)
    # A real spec keeps importlib.util.find_spec(name) working on the stub.
    mod.__spec__ = ModuleSpec(name, loader=None)
    return mod


def _install(name: str, mod: ModuleType) -> None:
    if name not in sys.modules:
        sys.modules[name] = mod


def _install_langchain_stub() -> None:
    core = _new_module("langchain_core")
    runnables = _new_module("langchain_core.runnables")
    config = _new_module("langchain_core.runnables.config")

    def ensure_config(config=None, **kw):
        return config or {}

    config.ensure_config = ensure_config  # type: ignore[attr-defined]
    runnables.config = config  # type: ignore[attr-defined]
    core.runnables = runnables  # type: ignore[attr-defined]

    _install("langchain_core", core)
    _install("langchain_core.runnables", runnables)
    _install("langchain_core.runnables.config", config)


def _install_llamaindex_stub() -> None:
    root = _new_module("llama_index")
    core = _new_module("llama_index.core")

    class Settings:
        callback_manager = MagicMock()

    core.Settings = Settings  # type: ignore[attr-defined]
    root.core = core  # type: ignore[attr-defined]

    _install("llama_index", root)
    _install("llama_index.core", core)


_CREWAI_EVENT_NAMES = [
    "CrewKickoffStartedEvent", "CrewKickoffCompletedEvent", "CrewKickoffFailedEvent",
    "AgentExecutionStartedEvent", "AgentExecutionCompletedEvent", "AgentExecutionErrorEvent",
    "TaskStartedEvent", "TaskCompletedEvent", "TaskFailedEvent",
    "ToolUsageStartedEvent", "ToolUsageFinishedEvent", "ToolUsageErrorEvent",
    "LLMCallStartedEvent", "LLMCallCompletedEvent", "LLMCallFailedEvent",
]


def _install_crewai_stub() -> None:
    root = _new_module("crewai")
    utilities = _new_module("crewai.utilities")
    events = _new_module("crewai.utilities.events")
    base_events = _new_module("crewai.utilities.events.base_events")

    class BaseEventListener:
        pass

    events.BaseEventListener = BaseEventListener  # type: ignore[attr-defined]
    # None matches "no global bus": the handler then skips auto-registration
    # and tests wire their own mock bus via setup_listeners().
    events.crewai_event_bus = None  # type: ignore[attr-defined]

    for event_name in _CREWAI_EVENT_NAMES:
        setattr(base_events, event_name, type(event_name, (), {}))

    events.base_events = base_events  # type: ignore[attr-defined]
    utilities.events = events  # type: ignore[attr-defined]
    root.utilities = utilities  # type: ignore[attr-defined]

    _install("crewai", root)
    _install("crewai.utilities", utilities)
    _install("crewai.utilities.events", events)
    _install("crewai.utilities.events.base_events", base_events)


def _install_ag2_stub() -> None:
    # ag2 publishes the `autogen` import namespace.
    mod = _new_module("autogen")

    class ConversableAgent:
        def __init__(self, *args, **kwargs):
            pass

    mod.ConversableAgent = ConversableAgent  # type: ignore[attr-defined]
    _install("autogen", mod)


def _install_autogen_agentchat_stub() -> None:
    mod = _new_module("autogen_agentchat")
    mod.EVENT_LOGGER_NAME = "autogen_agentchat.event"  # type: ignore[attr-defined]
    mod.TRACE_LOGGER_NAME = "autogen_agentchat.trace"  # type: ignore[attr-defined]
    _install("autogen_agentchat", mod)


def _install_agents_stub() -> None:
    mod = _new_module("agents")

    class TracingProcessor:
        pass

    class Trace:
        pass

    class Span:
        pass

    class AgentSpanData:
        pass

    class FunctionSpanData:
        pass

    class HandoffSpanData:
        pass

    class GuardrailSpanData:
        pass

    class GenerationSpanData:
        pass

    def add_trace_processor(processor):
        return None

    mod.TracingProcessor = TracingProcessor  # type: ignore[attr-defined]
    mod.Trace = Trace  # type: ignore[attr-defined]
    mod.Span = Span  # type: ignore[attr-defined]
    mod.AgentSpanData = AgentSpanData  # type: ignore[attr-defined]
    mod.FunctionSpanData = FunctionSpanData  # type: ignore[attr-defined]
    mod.HandoffSpanData = HandoffSpanData  # type: ignore[attr-defined]
    mod.GuardrailSpanData = GuardrailSpanData  # type: ignore[attr-defined]
    mod.GenerationSpanData = GenerationSpanData  # type: ignore[attr-defined]
    mod.add_trace_processor = add_trace_processor  # type: ignore[attr-defined]
    _install("agents", mod)


def _install_pageindex_stub() -> None:
    mod = _new_module("pageindex")

    class PageIndexClient:
        def __init__(self, api_key=None, **kwargs):
            self.api_key = api_key

        def chat_completions(self, messages, doc_id=None, **kwargs):
            raise RuntimeError("pageindex stub: no API access in tests")

        def get_tree(self, doc_id, **kwargs):
            raise RuntimeError("pageindex stub: no API access in tests")

    mod.PageIndexClient = PageIndexClient  # type: ignore[attr-defined]
    _install("pageindex", mod)


_install_langchain_stub()
_install_llamaindex_stub()
_install_crewai_stub()
_install_ag2_stub()
_install_autogen_agentchat_stub()
_install_agents_stub()
_install_pageindex_stub()
