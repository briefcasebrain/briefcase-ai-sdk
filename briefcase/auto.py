"""briefcase.auto: single-call global instrumentation.

Instruments AI frameworks and model-provider SDKs with one call. All patches
are idempotent and reversible via undo().

Usage:
    import briefcase

    briefcase.observe()          # destination from the environment
    briefcase.auto()             # everything that is installed

    # or one integration by name, returning its handler
    from briefcase.auto import auto, undo
    from briefcase.exporters import JSONLFileExporter

    auto("langchain", exporter=JSONLFileExporter("decisions.jsonl"))
    auto("openai", capture_content=True)
    auto("openai-agents", context_version="v2.1")

    # Undo one integration
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

Supported providers (see briefcase.integrations.providers; one decision
record per model call, content capture off unless capture_content is set):
    "openai"          chat.completions / responses / completions / embeddings
    "anthropic"       messages.create / parse / stream (and beta.messages)
    "google-genai"    models.generate_content / generate_content_stream
    "bedrock"         boto3 bedrock-runtime Converse / InvokeModel (+ streams)

auto("name") for a package that is not installed raises ImportError and
registers nothing: auto() never reports an integration as instrumented when
nothing was patched. auto() with no name skips what is not installed and
never raises for an absent package.
"""

import functools
import importlib.abc
import importlib.metadata
import importlib.util
import sys
import threading
from typing import Any, Dict, Iterable, Optional

from briefcase._logging import get_logger

logger = get_logger(__name__)

# Serializes auto()/undo() so two threads cannot both patch a framework.
_LOCK = threading.RLock()

# Registry: framework_name -> {handler, undos: [(obj, attr, orig), ...], cleanup}
_PATCHES: Dict[str, Dict[str, Any]] = {}

# Framework name -> import name whose presence means it is installed. The
# same module finishing its import is what a deferred (lazy) install waits
# for.
_FRAMEWORK_MODULES: Dict[str, str] = {
    "langchain": "langchain_core",
    "llamaindex": "llama_index.core",
    "ag2": "autogen",
    "crewai": "crewai",
    "openai-agents": "agents",
    "autogen": "autogen_agentchat",
    "pageindex": "pageindex",
}

# Import names too generic to trust on their own: the distribution must be
# installed as well, so a project's own ``agents`` package is not mistaken
# for the OpenAI Agents SDK.
_REQUIRED_DISTRIBUTION: Dict[str, str] = {"openai-agents": "openai-agents"}

_PROVIDER_NAMES = ("openai", "anthropic", "google-genai", "bedrock")

_SUPPORTED = frozenset(_FRAMEWORK_MODULES) | frozenset(_PROVIDER_NAMES)

# Options auto() with no name forwards to every integration. Anything else
# is specific to one handler and needs auto("name", ...).
_COMMON_OPTIONS = frozenset(["capture_content", "redact"])

# name -> options, for integrations waiting on their package's import, and
# the module names those waits are keyed on (kept in step by _sync_triggers;
# the import hook consults it on every import, so it is a plain set lookup).
_PENDING: Dict[str, Dict[str, Any]] = {}
_TRIGGERS: Dict[str, str] = {}


def auto(
    framework: Optional[str] = None,
    *,
    exporter: Any = None,
    context_version: Optional[str] = None,
    async_capture: bool = True,
    lazy: bool = False,
    skip: Iterable[str] = (),
    **kwargs: Any,
) -> Any:
    """Instrument one integration by name, or everything that is installed.

    ``auto("langchain")`` instruments that integration and returns its
    handler. ``auto()`` detects which supported frameworks and provider SDKs
    are installed (``importlib.util.find_spec``), instruments each, and
    returns ``{name: handler}``; packages that are absent are skipped, and
    one that fails to instrument is logged at WARNING and skipped.

    Idempotent: calling auto() again for the same integration returns the
    existing handler without re-patching.

    Args:
        framework:       A supported name (see module docstring), or None
                         for everything installed.
        exporter:        Briefcase exporter instance (e.g. JSONLFileExporter).
                         If None, falls back to BriefcaseConfig.get().exporter.
        context_version: Optional version tag added to all decision records.
        async_capture:   If True (default), export is fire-and-forget.
        lazy:            Only with no name. Instead of importing every
                         installed SDK now, instrument each one when the
                         application first imports it. Deferred entries are
                         reported as ``True`` in the returned mapping. Used
                         by ``briefcase exec`` so startup stays fast.
        skip:            Only with no name: integration names to leave alone.
        **kwargs:        With a name: forwarded to the handler constructor
                         (for example ``capture_content``, ``redact`` and
                         ``max_records``). With no name only
                         ``capture_content`` and ``redact`` are accepted,
                         since they are the options every integration takes.
                         ``capture_content`` defaults to ``"full"`` for
                         frameworks and to off for providers.

    Returns:
        The installed handler instance, or ``{name: handler_or_True}``.

    Raises:
        ValueError: If framework is not one of the supported values.
        ImportError: With a name, if that package is not installed.
            Nothing is patched or registered in that case.
        TypeError: With no name, for an option not every integration takes.
    """
    if framework is None:
        unknown = set(kwargs) - _COMMON_OPTIONS
        if unknown:
            raise TypeError(
                f"auto() with no name accepts only {sorted(_COMMON_OPTIONS)} "
                f"besides exporter/context_version/async_capture; got "
                f"{sorted(unknown)}. Pass handler-specific options to "
                "auto('<name>', ...)."
            )
        with _LOCK:
            return _auto_all(exporter, context_version, async_capture, lazy,
                             frozenset(skip), kwargs)

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

    if framework in _PROVIDER_NAMES:
        handler = _auto_provider(
            framework,
            exporter=exporter,
            context_version=context_version,
            async_capture=async_capture,
            **kwargs,
        )
        _discard_pending(framework)
        return handler

    dispatch = {
        "langchain": _auto_langchain,
        "llamaindex": _auto_llamaindex,
        "ag2": _auto_ag2,
        "crewai": _auto_crewai,
        "openai-agents": _auto_openai_agents,
        "autogen": _auto_autogen,
        "pageindex": _auto_pageindex,
    }
    handler = dispatch[framework](
        exporter=exporter,
        context_version=context_version,
        async_capture=async_capture,
        **kwargs,
    )
    _discard_pending(framework)
    return handler


def _discard_pending(name: str) -> None:
    if _PENDING.pop(name, None) is not None:
        _sync_triggers()


# Detect-and-instrument-everything

def _module_for(name: str) -> str:
    """Import name that marks ``name`` as installed, and whose import a
    deferred install waits for."""
    if name in _FRAMEWORK_MODULES:
        return _FRAMEWORK_MODULES[name]
    from briefcase.integrations.providers import PROVIDERS

    return PROVIDERS[name].detect


def _trigger_for(name: str) -> str:
    if name in _FRAMEWORK_MODULES:
        return _FRAMEWORK_MODULES[name]
    from briefcase.integrations.providers import PROVIDERS

    return PROVIDERS[name].trigger


def _is_installed(name: str) -> bool:
    """True when the package behind ``name`` can be imported. Never raises
    and never imports the package itself (only, for a dotted name, its
    parent)."""
    module = _module_for(name)
    try:
        if module in sys.modules:
            found = sys.modules[module] is not None
        else:
            found = importlib.util.find_spec(module) is not None
    except Exception:
        # find_spec raises for a missing parent package (ModuleNotFoundError)
        # and for a module object with no __spec__ (ValueError).
        found = False
    if not found:
        return False
    distribution = _REQUIRED_DISTRIBUTION.get(name)
    if distribution is not None:
        try:
            importlib.metadata.version(distribution)
        except Exception:
            return False
    return True


def _options_for(name: str, common: Dict[str, Any]) -> Dict[str, Any]:
    """Per-integration copy of the common options. Providers take a bool or
    a mode for ``capture_content``; framework handlers only the mode."""
    options = dict(common)
    if name in _FRAMEWORK_MODULES and "capture_content" in options:
        from briefcase.integrations.providers._base import normalize_capture_content

        options["capture_content"] = normalize_capture_content(options["capture_content"])
    return options


def _auto_all(exporter, context_version, async_capture, lazy, skip, common):
    result: Dict[str, Any] = {}
    for name in list(_FRAMEWORK_MODULES) + list(_PROVIDER_NAMES):
        if name in skip:
            continue
        if name in _PATCHES:
            result[name] = _PATCHES[name]["handler"]
            continue
        if not _is_installed(name):
            continue
        options = dict(
            _options_for(name, common),
            exporter=exporter,
            context_version=context_version,
            async_capture=async_capture,
        )
        if lazy and _trigger_for(name) not in sys.modules:
            _PENDING[name] = options
            _sync_triggers()
            result[name] = True
            continue
        handler = _instrument_quietly(name, options)
        if handler is not None:
            result[name] = handler
    return result


def _instrument_quietly(name: str, options: Dict[str, Any]) -> Any:
    """auto(name) that logs instead of raising; None when it failed."""
    try:
        return auto(name, **options)
    except Exception as exc:
        logger.warning(
            "briefcase.auto: %s is installed but could not be instrumented "
            "(%s: %s); skipping it.", name, type(exc).__name__, exc,
        )
        return None


# Deferred install: instrument a package when the application imports it

class _LoaderProxy(importlib.abc.Loader):
    """Delegates to the real loader, then runs the deferred installs that
    were waiting for this module."""

    def __init__(self, loader: Any, fullname: str) -> None:
        self._bc_loader = loader
        self._bc_fullname = fullname

    def create_module(self, spec: Any) -> Any:
        return self._bc_loader.create_module(spec)

    def exec_module(self, module: Any) -> None:
        self._bc_loader.exec_module(module)
        # Hand the module back its real loader so nothing downstream ever
        # sees this proxy.
        try:
            if getattr(module, "__loader__", None) is self:
                module.__loader__ = self._bc_loader
            spec = getattr(module, "__spec__", None)
            if spec is not None and spec.loader is self:
                spec.loader = self._bc_loader
        except Exception:
            pass
        _fire_pending(self._bc_fullname)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bc_loader, name)


class _DeferredInstallFinder(importlib.abc.MetaPathFinder):
    """Meta-path finder that finds nothing itself: for a module a deferred
    install is waiting on, it asks the remaining finders for the spec and
    wraps its loader."""

    def __init__(self) -> None:
        self._resolving = threading.local()

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname not in _TRIGGERS:
            return None
        active = getattr(self._resolving, "names", None)
        if active is None:
            active = self._resolving.names = set()
        if fullname in active:
            return None
        active.add(fullname)
        try:
            spec = importlib.util.find_spec(fullname)
        except Exception:
            return None
        finally:
            active.discard(fullname)
        if spec is None or spec.loader is None or not hasattr(spec.loader, "exec_module"):
            return None
        spec.loader = _LoaderProxy(spec.loader, fullname)
        return spec


_import_hook: Optional[_DeferredInstallFinder] = None


def _sync_triggers() -> None:
    """Rebuild _TRIGGERS from _PENDING and drop the import hook when
    nothing is waiting."""
    triggers: Dict[str, str] = {}
    for name in list(_PENDING):
        try:
            triggers[_trigger_for(name)] = name
        except Exception:  # pragma: no cover - registry lookups do not fail
            continue
    _TRIGGERS.clear()
    _TRIGGERS.update(triggers)
    if _TRIGGERS:
        _install_import_hook()
    elif _import_hook is not None:
        try:
            sys.meta_path.remove(_import_hook)
        except ValueError:
            pass


def _install_import_hook() -> None:
    global _import_hook
    if _import_hook is None:
        _import_hook = _DeferredInstallFinder()
    if _import_hook not in sys.meta_path:
        sys.meta_path.insert(0, _import_hook)


def _fire_pending(module_name: str) -> None:
    """Run the deferred installs waiting on ``module_name``. Called right
    after that module finished executing; never raises into the import."""
    try:
        with _LOCK:
            for name in [n for n in list(_PENDING) if _trigger_for(n) == module_name]:
                options = _PENDING.pop(name, None)
                if options is not None:
                    _sync_triggers()
                    _instrument_quietly(name, options)
    except Exception:
        logger.debug("deferred instrumentation failed", exc_info=True)


def pending() -> Dict[str, bool]:
    """Names whose instrumentation is deferred until their package is
    imported (see ``auto(lazy=True)``)."""
    with _LOCK:
        return {name: True for name in _PENDING}


def undo(framework: Optional[str] = None) -> None:
    """Remove patches for one or all integrations (frameworks and
    providers), including installs still deferred by ``auto(lazy=True)``.

    Args:
        framework: Integration name to undo. If None, undoes all of them.
    """
    with _LOCK:
        if framework is None:
            _PENDING.clear()
            for fw in list(_PATCHES.keys()):
                _undo_one(fw)
        else:
            _PENDING.pop(framework, None)
            _undo_one(framework)
        _sync_triggers()


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


def _auto_provider(name, exporter, context_version, async_capture, **kwargs):
    """Patch a model-provider SDK (briefcase.integrations.providers)."""
    from briefcase.integrations import providers

    instrumentor = providers.instrument(
        name,
        exporter=exporter,
        context_version=context_version,
        async_capture=async_capture,
        **kwargs,
    )
    _PATCHES[name] = {
        "handler": instrumentor,
        "undos": [],
        "cleanup": instrumentor.uninstall,
    }
    return instrumentor


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
