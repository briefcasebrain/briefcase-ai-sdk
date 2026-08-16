"""Framework integration handlers for automatic decision capture.

Each handler observes one AI framework (LangChain, LlamaIndex, CrewAI,
AutoGen, AG2, the OpenAI Agents SDK, or PageIndex) and turns its runtime
events into Briefcase decision records exported through the configured
exporter. Symbols resolve lazily so this package imports cleanly when no
framework is installed; a handler raises ImportError naming the matching
pip extra on first use if its framework is absent.

The ``*_hook`` names are module aliases for callers that prefer
``openai_agents_hook.install()`` over importing a handler class.
"""

import importlib
from typing import Dict, Tuple

__all__ = [
    "BriefcaseLangChainHandler",
    "BriefcaseLlamaIndexHandler",
    "OpenAIAgentsTracer",
    "openai_agents_hook",
    "PageIndexTracer",
    "PageIndexMCPObserver",
    "CrewAIEventListener",
    "AG2HookTracer",
    "ag2_hook",
    "AutoGenEventHandler",
    "autogen_hook",
]

_SYMBOLS: Dict[str, Tuple[str, str]] = {
    "BriefcaseLangChainHandler": (
        "briefcase.integrations.frameworks.langchain_handler",
        "BriefcaseLangChainHandler",
    ),
    "BriefcaseLlamaIndexHandler": (
        "briefcase.integrations.frameworks.llamaindex_handler",
        "BriefcaseLlamaIndexHandler",
    ),
    "OpenAIAgentsTracer": (
        "briefcase.integrations.frameworks.openai_agents_handler",
        "OpenAIAgentsTracer",
    ),
    "PageIndexTracer": (
        "briefcase.integrations.frameworks.pageindex_handler",
        "PageIndexTracer",
    ),
    "PageIndexMCPObserver": (
        "briefcase.integrations.frameworks.pageindex_mcp",
        "PageIndexMCPObserver",
    ),
    "CrewAIEventListener": (
        "briefcase.integrations.frameworks.crewai_handler",
        "CrewAIEventListener",
    ),
    "AG2HookTracer": (
        "briefcase.integrations.frameworks.ag2_handler",
        "AG2HookTracer",
    ),
    "AutoGenEventHandler": (
        "briefcase.integrations.frameworks.autogen_handler",
        "AutoGenEventHandler",
    ),
}

# Module aliases: the whole handler module under a shorter functional name.
_MODULE_ALIASES: Dict[str, str] = {
    "openai_agents_hook": "briefcase.integrations.frameworks.openai_agents_handler",
    "ag2_hook": "briefcase.integrations.frameworks.ag2_handler",
    "autogen_hook": "briefcase.integrations.frameworks.autogen_handler",
}


def __getattr__(name: str):
    if name in _MODULE_ALIASES:
        module = importlib.import_module(_MODULE_ALIASES[name])
        globals()[name] = module
        return module
    if name not in _SYMBOLS:
        raise AttributeError(name)
    module_name, symbol = _SYMBOLS[name]
    value = getattr(importlib.import_module(module_name), symbol)
    globals()[name] = value
    return value
