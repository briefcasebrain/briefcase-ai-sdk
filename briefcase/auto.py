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

A framework whose package is not installed raises ImportError and is not
registered: auto() never reports a framework as instrumented when nothing
was patched.
"""

import functools
import threading
from typing import Any, Dict, Optional

from briefcase._logging import get_logger

logger = get_logger(__name__)

# Serializes auto()/undo() so two threads cannot both patch a framework.
_LOCK = threading.RLock()

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
                         constructor (for example ``capture_content``,
                         ``redact`` and ``max_records``).

    Returns:
        The installed handler instance.

    Raises:
        ValueError: If framework is not one of the supported values.
        ImportError: If the framework's Python package is not installed.
            Nothing is patched or registered in that case.
    """
    if framework not in _SUPPORTED:
        raise ValueError(
            f"Unknown framework: {framework!r}. "
            f"Supported: {sorted(_SUPPORTED)}"
        )

    with _LOCK:
        return _auto_locked(framework, exporter, context_version, async_capture, kwargs)


def _auto_locked(framework, exporter, context_version, async_capture, kwargs):
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
    with _LOCK:
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
        except Exception as exc:
            logger.debug("_undo_one: ignored error: %s", exc)

    # Framework-specific teardown
    cleanup = patch_info.get("cleanup")
    if callable(cleanup):
        try:
            cleanup()
        except Exception as exc:
            logger.debug("_undo_one: ignored error: %s", exc)


# Per-framework implementations

def _missing(package: str, extra: str, exc: BaseException) -> ImportError:
    return ImportError(
        f"briefcase.auto: {package} is not installed, so nothing was "
        f"instrumented. Install with: pip install briefcase-ai[{extra}]"
    )


def _auto_langchain(exporter, context_version, async_capture, **kwargs):
    """Patch langchain_core.runnables.config.ensure_config to inject the handler."""
    from briefcase.integrations.frameworks.langchain_handler import BriefcaseLangChainHandler

    try:
        import langchain_core.runnables.config as _lc_config
    except ImportError as exc:
        raise _missing("langchain-core", "langchain", exc) from exc

    handler = BriefcaseLangChainHandler(
        exporter=exporter,
        context_version=context_version,
        async_capture=async_capture,
        **kwargs,
    )

    _orig_ensure_config = _lc_config.ensure_config

    def _patched_ensure_config(config=None, **kw):
        cfg = _orig_ensure_config(config, **kw)
        existing = cfg.get("callbacks")
        # callbacks is either a list of handlers or a BaseCallbackManager;
        # a manager gets the handler registered in place.
        if existing is not None and hasattr(existing, "add_handler"):
            if handler not in getattr(existing, "handlers", []):
                existing.add_handler(handler, inherit=True)
            return cfg
        callbacks = list(existing or [])
        if handler not in callbacks:
            callbacks.append(handler)
        cfg["callbacks"] = callbacks
        return cfg

    _lc_config.ensure_config = _patched_ensure_config
    undos = [(_lc_config, "ensure_config", _orig_ensure_config)]

    _PATCHES["langchain"] = {"handler": handler, "undos": undos}
    return handler


def _auto_llamaindex(exporter, context_version, async_capture, **kwargs):
    """Add the handler to llama_index.core.Settings.callback_manager."""
    from briefcase.integrations.frameworks.llamaindex_handler import BriefcaseLlamaIndexHandler

    try:
        from llama_index.core import Settings
    except ImportError as exc:
        raise _missing("llama-index-core", "llamaindex", exc) from exc

    handler = BriefcaseLlamaIndexHandler(
        exporter=exporter,
        context_version=context_version,
        async_capture=async_capture,
        **kwargs,
    )

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
            except Exception as exc:
                logger.debug("llamaindex undo: could not remove handler: %s", exc)

    _PATCHES["llamaindex"] = {"handler": handler, "undos": [], "cleanup": _cleanup}
    return handler


def _auto_ag2(exporter, context_version, async_capture, **kwargs):
    """Patch autogen.ConversableAgent.__init__ to instrument every new agent."""
    from briefcase.integrations.frameworks.ag2_handler import AG2HookTracer

    try:
        import autogen as _ag2_mod
    except ImportError as exc:
        raise _missing("ag2", "ag2", exc) from exc

    tracer = AG2HookTracer(
        exporter=exporter,
        context_version=context_version,
        async_capture=async_capture,
        **kwargs,
    )

    _orig_init = _ag2_mod.ConversableAgent.__init__

    def _patched_init(self_agent, *args, **kw):
        _orig_init(self_agent, *args, **kw)
        try:
            tracer.instrument(self_agent)
        except Exception as exc:
            logger.debug("ag2: could not instrument agent: %s", exc)

    _ag2_mod.ConversableAgent.__init__ = _patched_init
    undos = [(_ag2_mod.ConversableAgent, "__init__", _orig_init)]

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
        **kwargs,
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
        **kwargs,
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

    try:
        import pageindex as _pi_mod
    except ImportError as exc:
        raise _missing("pageindex", "pageindex", exc) from exc

    tracer = PageIndexTracer(
        exporter=exporter,
        context_version=context_version,
        async_capture=async_capture,
        **kwargs,
    )

    _orig_chat = _pi_mod.PageIndexClient.chat_completions

    def _patched_chat(client_self, messages, doc_id=None, **kw):
        # The calling client and its unpatched method travel with the call.
        # Nothing is written to the shared tracer, so concurrent calls on
        # different clients cannot be attributed to each other, and the
        # tracer never re-enters this patched method.
        return tracer._capture_chat(
            client_self,
            functools.partial(_orig_chat, client_self),
            messages,
            doc_id,
            **kw,
        )

    _pi_mod.PageIndexClient.chat_completions = _patched_chat
    undos = [(_pi_mod.PageIndexClient, "chat_completions", _orig_chat)]

    _PATCHES["pageindex"] = {"handler": tracer, "undos": undos}
    return tracer
