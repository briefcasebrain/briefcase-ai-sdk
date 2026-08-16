"""briefcase.auto: single-call global framework instrumentation.

Instruments a supported AI framework with one call and returns the handler
instance. All patches are idempotent and reversible via undo().

Usage:
    from briefcase.auto import auto, undo
    from briefcase.exporters import JSONLFileExporter

    auto("langchain", exporter=JSONLFileExporter("decisions.jsonl"))
    auto("crewai")
    auto("openai-agents", context_version="v2.1")

    # Undo one framework
    undo("langchain")
    # Undo all
    undo()

Supported frameworks:
    "langchain"       patches langchain_core.runnables.config.ensure_config
    "llamaindex"      adds a handler to llama_index.core.Settings.callback_manager
    "ag2"             patches autogen.ConversableAgent.__init__ to auto-instrument
    "crewai"          instantiates CrewAIEventListener (auto-registers on the bus)
    "openai-agents"   delegates to openai_agents_handler.install()
    "autogen"         delegates to autogen_handler.install()
    "pageindex"       patches pageindex.PageIndexClient.chat_completions
"""

from typing import Any, Dict, Optional

# Registry: framework_name -> {handler, undos: [(obj, attr, orig), ...], cleanup}
_PATCHES: Dict[str, Dict[str, Any]] = {}

_SUPPORTED = frozenset(
    ["langchain", "llamaindex", "ag2", "crewai", "openai-agents", "autogen", "pageindex"]
)


def auto(
    framework: str,
    *,
    exporter: Any = None,
    context_version: Optional[str] = None,
    async_capture: bool = True,
    **kwargs: Any,
) -> Any:
    """Instrument a framework globally. Returns the handler instance.

    Idempotent: calling auto() again for the same framework returns the
    existing handler without re-patching.

    Args:
        framework:       One of the supported framework names (see module docstring).
        exporter:        Briefcase exporter instance (e.g. JSONLFileExporter).
                         If None, falls back to BriefcaseConfig.get().exporter.
        context_version: Optional version tag added to all decision records.
        async_capture:   If True (default), export is fire-and-forget.
        **kwargs:        Additional keyword arguments forwarded to the handler
                         constructor.

    Returns:
        The installed handler instance.

    Raises:
        ValueError: If framework is not one of the supported values.
        ImportError: If the framework's Python package is not installed.
    """
    if framework not in _SUPPORTED:
        raise ValueError(
            f"Unknown framework: {framework!r}. "
            f"Supported: {sorted(_SUPPORTED)}"
        )

    # Idempotency: return the existing handler when already patched
    if framework in _PATCHES:
        return _PATCHES[framework]["handler"]

    dispatch = {
        "langchain": _auto_langchain,
        "llamaindex": _auto_llamaindex,
        "ag2": _auto_ag2,
        "crewai": _auto_crewai,
        "openai-agents": _auto_openai_agents,
        "autogen": _auto_autogen,
        "pageindex": _auto_pageindex,
    }
    return dispatch[framework](
        exporter=exporter,
        context_version=context_version,
        async_capture=async_capture,
        **kwargs,
    )


def undo(framework: Optional[str] = None) -> None:
    """Remove patches for one or all frameworks.

    Args:
        framework: Framework name to undo. If None, undoes all frameworks.
    """
    if framework is None:
        for fw in list(_PATCHES.keys()):
            _undo_one(fw)
    else:
        _undo_one(framework)


# Private undo helper

def _undo_one(framework: str) -> None:
    patch_info = _PATCHES.pop(framework, None)
    if patch_info is None:
        return

    for obj, attr, original in patch_info.get("undos", []):
        try:
            setattr(obj, attr, original)
        except Exception:
            pass

    # Framework-specific teardown
    cleanup = patch_info.get("cleanup")
    if callable(cleanup):
        try:
            cleanup()
        except Exception:
            pass


# Per-framework implementations

def _auto_langchain(exporter, context_version, async_capture, **kwargs):
    """Patch langchain_core.runnables.config.ensure_config to inject the handler."""
    from briefcase.integrations.frameworks.langchain_handler import BriefcaseLangChainHandler

    handler = BriefcaseLangChainHandler(
        exporter=exporter,
        context_version=context_version,
        async_capture=async_capture,
        **kwargs,
    )

    undos = []
    try:
        import langchain_core.runnables.config as _lc_config

        _orig_ensure_config = _lc_config.ensure_config

        def _patched_ensure_config(config=None, **kw):
            cfg = _orig_ensure_config(config, **kw)
            callbacks = list(cfg.get("callbacks") or [])
            if handler not in callbacks:
                callbacks.append(handler)
            cfg["callbacks"] = callbacks
            return cfg

        _lc_config.ensure_config = _patched_ensure_config
        undos.append((_lc_config, "ensure_config", _orig_ensure_config))
    except Exception:
        pass  # langchain not importable; the handler is still returned

    _PATCHES["langchain"] = {"handler": handler, "undos": undos}
    return handler


def _auto_llamaindex(exporter, context_version, async_capture, **kwargs):
    """Add the handler to llama_index.core.Settings.callback_manager."""
    from briefcase.integrations.frameworks.llamaindex_handler import BriefcaseLlamaIndexHandler

    handler = BriefcaseLlamaIndexHandler(
        exporter=exporter,
        context_version=context_version,
        async_capture=async_capture,
        **kwargs,
    )

    undos = []
    cleanup = None
    try:
        from llama_index.core import Settings

        Settings.callback_manager.add_handler(handler)

        def _cleanup():
            try:
                Settings.callback_manager.remove_handler(handler)
            except Exception:
                # Fall back to filtering manually when remove_handler is absent
                try:
                    existing = Settings.callback_manager.handlers
                    Settings.callback_manager.handlers = [
                        h for h in existing if h is not handler
                    ]
                except Exception:
                    pass

        cleanup = _cleanup
    except Exception:
        pass  # llama_index not importable

    _PATCHES["llamaindex"] = {"handler": handler, "undos": undos, "cleanup": cleanup}
    return handler


def _auto_ag2(exporter, context_version, async_capture, **kwargs):
    """Patch autogen.ConversableAgent.__init__ to instrument every new agent."""
    from briefcase.integrations.frameworks.ag2_handler import AG2HookTracer

    tracer = AG2HookTracer(
        exporter=exporter,
        context_version=context_version,
        async_capture=async_capture,
        **kwargs,
    )

    undos = []
    try:
        import autogen as _ag2_mod

        _orig_init = _ag2_mod.ConversableAgent.__init__

        def _patched_init(self_agent, *args, **kw):
            _orig_init(self_agent, *args, **kw)
            try:
                tracer.instrument(self_agent)
            except Exception:
                pass

        _ag2_mod.ConversableAgent.__init__ = _patched_init
        undos.append((_ag2_mod.ConversableAgent, "__init__", _orig_init))
    except Exception:
        pass  # ag2 not importable

    _PATCHES["ag2"] = {"handler": tracer, "undos": undos}
    return tracer


def _auto_crewai(exporter, context_version, async_capture, **kwargs):
    """Instantiate CrewAIEventListener (auto-registers on the event bus)."""
    from briefcase.integrations.frameworks.crewai_handler import CrewAIEventListener

    listener = CrewAIEventListener(
        exporter=exporter,
        context_version=context_version,
        async_capture=async_capture,
        **kwargs,
    )

    _PATCHES["crewai"] = {"handler": listener, "undos": []}
    return listener


def _auto_openai_agents(exporter, context_version, async_capture, **kwargs):
    """Delegate to openai_agents_handler.install()."""
    from briefcase.integrations.frameworks.openai_agents_handler import install

    tracer = install(
        context_version=context_version,
        async_capture=async_capture,
        exporter=exporter,
    )

    _PATCHES["openai-agents"] = {"handler": tracer, "undos": []}
    return tracer


def _auto_autogen(exporter, context_version, async_capture, **kwargs):
    """Delegate to autogen_handler.install(); undo detaches the handler."""
    from briefcase.integrations.frameworks import autogen_handler

    handler = autogen_handler.install(
        context_version=context_version,
        async_capture=async_capture,
        exporter=exporter,
    )

    _PATCHES["autogen"] = {
        "handler": handler,
        "undos": [],
        "cleanup": autogen_handler.uninstall,
    }
    return handler


def _auto_pageindex(exporter, context_version, async_capture, **kwargs):
    """Patch pageindex.PageIndexClient.chat_completions to auto-capture."""
    from briefcase.integrations.frameworks.pageindex_handler import PageIndexTracer

    tracer = PageIndexTracer(
        exporter=exporter,
        context_version=context_version,
        async_capture=async_capture,
        **kwargs,
    )

    undos = []
    try:
        import pageindex as _pi_mod

        _orig_chat = _pi_mod.PageIndexClient.chat_completions
        _orig_init = _pi_mod.PageIndexClient.__init__

        def _patched_init(client_self, *args, **kw):
            _orig_init(client_self, *args, **kw)
            # Point the tracer at this client for API calls
            tracer._client = client_self

        def _patched_chat(client_self, messages, doc_id=None, **kw):
            # Point the tracer at the calling client instance
            tracer._client = client_self
            return tracer.chat_completions(messages=messages, doc_id=doc_id, **kw)

        _pi_mod.PageIndexClient.__init__ = _patched_init
        _pi_mod.PageIndexClient.chat_completions = _patched_chat
        undos.append((_pi_mod.PageIndexClient, "__init__", _orig_init))
        undos.append((_pi_mod.PageIndexClient, "chat_completions", _orig_chat))
    except Exception:
        pass  # pageindex not importable

    _PATCHES["pageindex"] = {"handler": tracer, "undos": undos}
    return tracer
