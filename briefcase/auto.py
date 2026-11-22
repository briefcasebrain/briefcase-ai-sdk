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
    "aiobotocore"     the same operations through aiobotocore (async)

status() reports, per integration, what is patched, what was skipped and
why, the SDK version found and the versions tested, and the known capture
limits. A framework running on an instrumented provider SDK produces two
records per model call; they are linked (parent_run_id on the provider
record) and auto(dedupe=...) can drop one side.

auto("name") for a package that is not installed raises ImportError and
registers nothing: auto() never reports an integration as instrumented when
nothing was patched. auto() with no name skips what is not installed and
never raises for an absent package.
"""

import functools
import importlib.abc
import importlib.metadata
import importlib.util
import os
import sys
import threading
from typing import Any, Dict, Iterable, List, Optional

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

_PROVIDER_NAMES = ("openai", "anthropic", "google-genai", "bedrock", "aiobotocore")

_SUPPORTED = frozenset(_FRAMEWORK_MODULES) | frozenset(_PROVIDER_NAMES)

# Options auto() with no name forwards to every integration. Anything else
# is specific to one handler and needs auto("name", ...).
_COMMON_OPTIONS = frozenset(["capture_content", "redact"])

# name -> options, for integrations waiting on their package's import, and
# the module names those waits are keyed on (kept in step by _sync_triggers;
# the import hook consults it on every import, so it is a plain set lookup).
_PENDING: Dict[str, Dict[str, Any]] = {}
_TRIGGERS: Dict[str, str] = {}
# Trigger modules whose import finished and whose deferred installs have not
# run yet (see _fire_pending).
_READY: List[str] = []
# name -> "ExceptionType: message" of the last failed attempt, for status().
_FAILED: Dict[str, str] = {}

ENV_CAPTURE_CONTENT = "BRIEFCASE_CAPTURE_CONTENT"
_warned_framework_content = False


def _after_fork_in_child() -> None:
    # Another thread may have been inside auto() / undo() at the fork; its
    # copy of the lock would stay held forever in the child, and every
    # deferred install would wait on it.
    global _LOCK
    _LOCK = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def auto(
    framework: Optional[str] = None,
    *,
    exporter: Any = None,
    context_version: Optional[str] = None,
    async_capture: bool = True,
    lazy: bool = False,
    skip: Iterable[str] = (),
    dedupe: Optional[str] = None,
    **kwargs: Any,
) -> Any:
    """Instrument one integration by name, or everything that is installed.

    ``auto("langchain")`` instruments that integration and returns its
    handler. ``auto()`` detects which supported frameworks and provider SDKs
    are installed (``importlib.util.find_spec``), instruments each, and
    returns ``{name: handler}``; packages that are absent are skipped, and
    one that fails to instrument is logged at WARNING and skipped.

    Idempotent: calling auto() again for the same integration with the same
    (or no) options returns the existing handler without re-patching. An
    explicit ``auto("name", ...)`` whose options differ from the active
    configuration is never ignored: the integration is undone and
    instrumented again with the new options on top of the old ones (logged
    at WARNING, since a handler returned earlier is then detached).

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
        dedupe:          What to do when a framework handler and a provider
                         integration both record the same model call
                         (LangChain on OpenAI, ...). ``"none"`` (the default)
                         keeps both; the provider record carries
                         ``parent_run_id`` / ``parent_framework`` so they can
                         be joined, and token totals must then be summed
                         over one side only. ``"provider"`` drops the
                         provider record of a call made under a framework LLM
                         run; ``"framework"`` drops the framework's LLM-level
                         record when a provider record was emitted for it.
                         Process-wide; ``None`` leaves the current mode.
        **kwargs:        With a name: forwarded to the handler constructor
                         (for example ``capture_content``, ``redact`` and
                         ``max_records``). With no name only
                         ``capture_content`` and ``redact`` are accepted,
                         since they are the options every integration takes.
                         ``capture_content`` defaults to ``"full"`` for
                         frameworks and to off for providers; when it is not
                         passed and ``BRIEFCASE_CAPTURE_CONTENT`` is set
                         (``none`` / ``hash`` / ``full``), that value is used
                         for both. A framework installed with the ``"full"``
                         default logs one WARNING saying so.

    Returns:
        The installed handler instance, or ``{name: handler_or_True}``.

    Raises:
        ValueError: If framework is not one of the supported values.
        ImportError: With a name, if that package is not installed.
            Nothing is patched or registered in that case.
        TypeError: With no name, for an option not every integration takes.
    """
    if dedupe is not None:
        from briefcase.integrations.providers import _correlation

        _correlation.set_dedupe(dedupe)
    if framework is None:
        unknown = set(kwargs) - _COMMON_OPTIONS
        if unknown:
            raise TypeError(
                f"auto() with no name accepts only {sorted(_COMMON_OPTIONS)} "
                f"besides exporter/context_version/async_capture; got "
                f"{sorted(unknown)}. Pass handler-specific options to "
                "auto('<name>', ...)."
            )
        skip = frozenset([skip] if isinstance(skip, str) else skip)
        unknown_skip = sorted(skip - _SUPPORTED)
        if unknown_skip:
            logger.warning(
                "briefcase.auto: skip names %s are not integrations and were ignored. "
                "Supported: %s", unknown_skip, sorted(_SUPPORTED),
            )
        with _LOCK:
            try:
                return _auto_all(exporter, context_version, async_capture, lazy, skip, kwargs)
            finally:
                _drain_ready()

    if framework not in _SUPPORTED:
        raise ValueError(
            f"Unknown framework: {framework!r}. "
            f"Supported: {sorted(_SUPPORTED)}"
        )

    with _LOCK:
        try:
            return _auto_locked(framework, exporter, context_version, async_capture, kwargs)
        finally:
            _drain_ready()


def _explicit(exporter, context_version, async_capture, kwargs) -> Dict[str, Any]:
    """The options a caller actually stated (defaults are not statements)."""
    options = dict(kwargs)
    if exporter is not None:
        options["exporter"] = exporter
    if context_version is not None:
        options["context_version"] = context_version
    if async_capture is not True:
        options["async_capture"] = async_capture
    return options


def _differs(active: Dict[str, Any], explicit: Dict[str, Any]) -> List[str]:
    changed = []
    for key, value in explicit.items():
        current = active.get(key, _UNSET)
        if key == "capture_content" and current is not _UNSET:
            from briefcase.integrations.providers._base import normalize_capture_content
            try:
                if normalize_capture_content(current) == normalize_capture_content(value):
                    continue
            except ValueError:
                pass
        if current is value:
            continue
        try:
            if current is not _UNSET and current == value:
                continue
        except Exception:
            pass
        changed.append(key)
    return sorted(changed)


_UNSET: Any = object()


def _framework_content_default(framework: str, kwargs: Dict[str, Any]) -> None:
    """Resolve ``capture_content`` when it was not passed: the environment
    variable if set, else the integration's own default. A framework left
    on its full-content default says so once, loudly."""
    global _warned_framework_content
    if "capture_content" in kwargs:
        return
    raw = (os.environ.get(ENV_CAPTURE_CONTENT) or "").strip().lower()
    if raw in ("none", "hash", "full"):
        kwargs["capture_content"] = raw
        return
    if raw:
        logger.warning(
            "briefcase.auto: ignoring %s=%r (expected none, hash or full).",
            ENV_CAPTURE_CONTENT, raw,
        )
    if framework in _FRAMEWORK_MODULES and not _warned_framework_content:
        _warned_framework_content = True
        message = (
            f"briefcase.auto({framework!r}): framework handlers record FULL prompt and "
            "completion content by default. (Provider integrations and `briefcase exec` "
            "record none.) To turn it off pass capture_content='none' (or 'hash') to "
            f"auto(), or set {ENV_CAPTURE_CONTENT}=none; pass capture_content='full' to "
            "keep it and silence this warning."
        )
        logger.warning(message)
        _visible_warning(message)


def _visible_warning(message: str) -> None:
    """Also say it through ``warnings``: briefcase's logger is silent unless
    the application configured logging, and this must not be missed. Never
    raises, also under ``-W error``."""
    try:
        import warnings

        warnings.warn(message, UserWarning, stacklevel=5)
    except Exception:
        pass


def _auto_locked(framework, exporter, context_version, async_capture, kwargs):
    explicit = _explicit(exporter, context_version, async_capture, kwargs)
    base: Dict[str, Any] = {}
    if framework in _PATCHES:
        active = _PATCHES[framework].get("options", {})
        changed = _differs(active, explicit)
        if not changed:
            # Idempotency: same configuration, same handler.
            return _PATCHES[framework]["handler"]
        logger.warning(
            "briefcase.auto(%r): already instrumented, but called again with different "
            "options (%s); re-instrumenting with the new values. A handler returned by "
            "the earlier call is detached.", framework, ", ".join(changed),
        )
        base = dict(active)
        _undo_one(framework)
    elif framework in _PENDING:
        # A deferred install (auto(lazy=True), briefcase exec) is waiting:
        # its options are the base the explicit ones are applied on.
        # Taken off the waiting list first: importing the package below
        # must not fire the deferred install a second time.
        base = dict(_PENDING.pop(framework))
        _sync_triggers()

    options = dict(base, **explicit)
    exporter = options.pop("exporter", None)
    context_version = options.pop("context_version", None)
    async_capture = options.pop("async_capture", True)
    kwargs = options
    _framework_content_default(framework, kwargs)
    stored = dict(kwargs, exporter=exporter, context_version=context_version,
                  async_capture=async_capture)

    try:
        handler = _install(framework, exporter, context_version, async_capture, kwargs)
    except Exception as exc:
        _FAILED[framework] = f"{type(exc).__name__}: {exc}"
        raise
    _FAILED.pop(framework, None)
    if framework in _PATCHES:
        _PATCHES[framework]["options"] = stored
    _discard_pending(framework)
    return handler


def _install(framework, exporter, context_version, async_capture, kwargs):
    if framework in _PROVIDER_NAMES:
        return _auto_provider(
            framework,
            exporter=exporter,
            context_version=context_version,
            async_capture=async_capture,
            **kwargs,
        )

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
        if name not in _PATCHES and not _is_installed(name):
            continue
        options = dict(
            _options_for(name, common),
            exporter=exporter,
            context_version=context_version,
            async_capture=async_capture,
        )
        if lazy and name not in _PATCHES and _trigger_for(name) not in sys.modules:
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
        options = dict(options)
        return _auto_locked(
            name, options.pop("exporter", None), options.pop("context_version", None),
            options.pop("async_capture", True), options,
        )
    except Exception as exc:
        _FAILED[name] = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "briefcase.auto: %s is installed but could not be instrumented "
            "(%s: %s); skipping it.", name, type(exc).__name__, exc,
        )
        return None


# Deferred install: instrument a package when the application imports it

class _LoaderProxy(importlib.abc.Loader):
    """Delegates to the real loader, then runs the deferred installs that
    were waiting for this module. Everything else (``get_data``,
    ``get_resource_reader``, ``is_package`` ...) is passed to the real
    loader, and the module is given its real loader back as soon as it has
    executed, also when executing it raised."""

    def __init__(self, loader: Any, fullname: str) -> None:
        self._bc_loader = loader
        self._bc_fullname = fullname

    def create_module(self, spec: Any) -> Any:
        return self._bc_loader.create_module(spec)

    def _bc_restore(self, module: Any) -> None:
        try:
            if getattr(module, "__loader__", None) is self:
                module.__loader__ = self._bc_loader
            spec = getattr(module, "__spec__", None)
            if spec is not None and spec.loader is self:
                spec.loader = self._bc_loader
        except Exception:
            pass

    def exec_module(self, module: Any) -> None:
        try:
            self._bc_loader.exec_module(module)
        finally:
            self._bc_restore(module)
        _fire_pending(self._bc_fullname)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bc_loader, name)


class _DeferredInstallFinder(importlib.abc.MetaPathFinder):
    """Meta-path finder that finds nothing itself: for a module a deferred
    install is waiting on, it asks the *other* finders for the spec (in
    their order, with the arguments it was given) and wraps the loader.

    It never answers for a module nobody is waiting on, never imports
    anything, and hands back ``None`` (so the import proceeds as if it were
    not there) whenever something is unexpected."""

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname not in _TRIGGERS:
            return None
        if target is not None:
            # importlib.reload(): the module is already instrumented or will
            # be when the first import finishes; do not wrap a reload.
            return None
        try:
            spec = None
            for finder in list(sys.meta_path):
                if finder is self or isinstance(finder, _DeferredInstallFinder):
                    continue
                find = getattr(finder, "find_spec", None)
                if find is None:
                    continue
                spec = find(fullname, path, target)
                if spec is not None:
                    break
            if spec is None or spec.loader is None:
                return None
            if isinstance(spec.loader, _LoaderProxy) or not hasattr(spec.loader, "exec_module"):
                return None
            spec.loader = _LoaderProxy(spec.loader, fullname)
            return spec
        except Exception:
            return None


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
    after that module finished executing, while this thread still holds the
    module's import lock; never raises into the import.

    It must not *wait* for ``_LOCK`` here: another thread may hold it (in
    ``auto()``) while trying to import this very module, and each would
    wait for the other forever. So the module is noted as ready and the
    installs run now only if the lock is free; otherwise the thread that
    holds it runs them before releasing it (``_drain_ready``)."""
    try:
        _READY.append(module_name)
        if _LOCK.acquire(blocking=False):
            try:
                _drain_ready()
            finally:
                _LOCK.release()
    except Exception:
        logger.debug("deferred instrumentation failed", exc_info=True)


def _drain_ready() -> None:
    """Run the deferred installs of every module noted as ready. Caller
    holds ``_LOCK``. Never raises."""
    try:
        while _READY:
            module_name = _READY.pop(0)
            for name in [n for n in list(_PENDING) if _trigger_for(n) == module_name]:
                options = _PENDING.pop(name, None)
                _sync_triggers()
                if options is None or name in _PATCHES:
                    continue
                _instrument_quietly(name, dict(options))
    except Exception:
        logger.debug("deferred instrumentation failed", exc_info=True)


def pending() -> Dict[str, bool]:
    """Names whose instrumentation is deferred until their package is
    imported (see ``auto(lazy=True)``)."""
    with _LOCK:
        return {name: True for name in _PENDING}


def status() -> Dict[str, Any]:
    """What is instrumented right now, and what is not and why.

    Returns ``{"dedupe": mode, "lazy_import_hook": bool, "integrations":
    {name: entry}}`` with one entry per supported integration:

    - ``kind``: ``"framework"`` or ``"provider"``.
    - ``state``: ``"patched"``, ``"partial"`` (some call paths were skipped
      because the installed SDK differs from what the patches expect),
      ``"pending"`` (waiting for the package to be imported, see
      ``auto(lazy=True)``), ``"failed"`` (installed, could not be
      instrumented; ``reason`` says why), ``"not_installed"`` or
      ``"inactive"`` (installed, ``auto()`` not called for it).
    - Providers also report ``sdk_version``, ``tested_versions`` (the
      versions the test-suite ran against; any other version is checked
      structurally at install time but was not run), ``reported_versions``
      (said to work by users, not re-run here), ``patched`` (the
      targets replaced), ``skipped`` (``{call path: reason}``), ``limits``
      (known gaps in what is captured) and ``options``.

    Read-only and safe to call at any time; never imports an SDK.
    """
    from briefcase.integrations.providers import _correlation

    with _LOCK:
        integrations: Dict[str, Any] = {}
        for name in list(_FRAMEWORK_MODULES) + list(_PROVIDER_NAMES):
            kind = "provider" if name in _PROVIDER_NAMES else "framework"
            entry: Dict[str, Any] = {"kind": kind}
            info = _PATCHES.get(name)
            if info is not None:
                handler = info.get("handler")
                entry["state"] = "patched"
                if kind == "provider":
                    skipped = dict(getattr(handler, "skipped", {}) or {})
                    entry.update(
                        sdk_version=getattr(handler, "sdk_version", None),
                        tested_versions=list(getattr(handler, "tested_versions", ()) or ()),
                        reported_versions=list(getattr(handler, "reported_versions", ()) or ()),
                        patched=list(getattr(handler, "patched", ()) or ()),
                        skipped=skipped,
                        limits=list(getattr(handler, "limits", ()) or ()),
                        options=dict(getattr(handler, "options", {}) or {}),
                    )
                    options = getattr(handler, "capture_options", None)
                    entry["capture_content"] = getattr(options, "capture_content", None)
                    if skipped:
                        entry["state"] = "partial"
                else:
                    entry["capture_content"] = info.get("options", {}).get(
                        "capture_content", "full")
            elif name in _PENDING:
                entry["state"] = "pending"
                entry["reason"] = f"waiting for 'import {_trigger_for(name)}'"
            elif name in _FAILED:
                entry["state"] = "failed"
                entry["reason"] = _FAILED[name]
            elif not _is_installed(name):
                entry["state"] = "not_installed"
            else:
                entry["state"] = "inactive"
            integrations[name] = entry
        return {
            "dedupe": _correlation.get_dedupe(),
            "lazy_import_hook": _import_hook is not None and _import_hook in sys.meta_path,
            "integrations": integrations,
        }


def format_status(report: Optional[Dict[str, Any]] = None) -> List[str]:
    """``status()`` as lines of text (for ``briefcase doctor`` and logs)."""
    report = status() if report is None else report
    lines = [f"auto-instrumentation (dedupe={report.get('dedupe')})"]
    for name, entry in report.get("integrations", {}).items():
        state = entry.get("state")
        if state in ("not_installed", "inactive"):
            continue
        line = f"  {name}: {state}"
        if entry.get("sdk_version"):
            tested = ", ".join(entry.get("tested_versions") or []) or "no version"
            line += f" (sdk {entry['sdk_version']}; tested with {tested}"
            if entry.get("reported_versions"):
                line += "; reported working with " + ", ".join(entry["reported_versions"])
            if entry["sdk_version"] not in (entry.get("tested_versions") or []):
                line += "; THIS VERSION WAS NOT TESTED, only checked structurally"
            line += ")"
        if entry.get("reason"):
            line += f" - {entry['reason']}"
        lines.append(line)
        for path, reason in (entry.get("skipped") or {}).items():
            lines.append(f"    NOT recorded: {path}: {reason}")
        for limit in entry.get("limits") or []:
            lines.append(f"    limit: {limit}")
    return lines


def undo(framework: Optional[str] = None) -> None:
    """Remove patches for one or all integrations (frameworks and
    providers), including installs still deferred by ``auto(lazy=True)``.

    Args:
        framework: Integration name to undo. If None, undoes all of them.
    """
    with _LOCK:
        if framework is None:
            _PENDING.clear()
            _READY.clear()
            _FAILED.clear()
            for fw in list(_PATCHES.keys()):
                _undo_one(fw)
        else:
            _PENDING.pop(framework, None)
            _FAILED.pop(framework, None)
            _undo_one(framework)
        _sync_triggers()


# Private undo helper

def _undo_one(framework: str) -> None:
    patch_info = _PATCHES.pop(framework, None)
    if patch_info is None:
        return

    # Our wrappers go quiet first, so one that cannot be removed (below),
    # or that a framework object captured, does nothing from here on.
    switch = patch_info.get("switch")
    if switch is not None:
        switch["active"] = False

    for undo_entry in patch_info.get("undos", []):
        obj, attr, original = undo_entry[:3]
        wrapper = undo_entry[3] if len(undo_entry) > 3 else None
        try:
            if wrapper is not None and getattr(obj, attr, None) is not wrapper:
                # Another library patched the same attribute after us:
                # putting the original back would silently remove its patch.
                logger.warning(
                    "briefcase.auto.undo(%r): %s.%s was patched again by another library "
                    "after briefcase; left as it is. The briefcase wrapper beneath it is "
                    "disabled.", framework, getattr(obj, "__name__", obj), attr,
                )
                continue
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
    """Patch langchain_core.runnables.config.ensure_config (and the bindings
    LangChain's already-imported modules hold) to inject the handler."""
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
    switch = {"active": True}

    @functools.wraps(_orig_ensure_config)
    def _patched_ensure_config(config=None, **kw):
        cfg = _orig_ensure_config(config, **kw)
        if not switch["active"]:
            return cfg
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
    undos = [(_lc_config, "ensure_config", _orig_ensure_config, _patched_ensure_config)]
    # LangChain's own modules bind the function at import time
    # (``from langchain_core.runnables.config import ensure_config``), so
    # every module already imported keeps calling the original unless its
    # binding is replaced too. Modules imported from now on bind the
    # patched function; after undo() it stays there but is switched off.
    for module_name, module in list(sys.modules.items()):
        if module is None or module is _lc_config or not module_name.startswith("langchain"):
            continue
        try:
            if vars(module).get("ensure_config") is _orig_ensure_config:
                module.ensure_config = _patched_ensure_config
                undos.append(
                    (module, "ensure_config", _orig_ensure_config, _patched_ensure_config))
        except Exception as exc:
            logger.debug("langchain: could not rebind ensure_config in %s: %s", module_name, exc)

    _PATCHES["langchain"] = {"handler": handler, "undos": undos, "switch": switch}
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
    switch = {"active": True}

    @functools.wraps(_orig_init)
    def _patched_init(self_agent, *args, **kw):
        _orig_init(self_agent, *args, **kw)
        if not switch["active"]:
            return
        try:
            tracer.instrument(self_agent)
        except Exception as exc:
            logger.debug("ag2: could not instrument agent: %s", exc)

    _ag2_mod.ConversableAgent.__init__ = _patched_init
    undos = [(_ag2_mod.ConversableAgent, "__init__", _orig_init, _patched_init)]

    _PATCHES["ag2"] = {"handler": tracer, "undos": undos, "switch": switch}
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
    switch = {"active": True}

    @functools.wraps(_orig_chat)
    def _patched_chat(client_self, messages, doc_id=None, **kw):
        if not switch["active"]:
            return _orig_chat(client_self, messages, doc_id, **kw)
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
    undos = [(_pi_mod.PageIndexClient, "chat_completions", _orig_chat, _patched_chat)]

    _PATCHES["pageindex"] = {"handler": tracer, "undos": undos, "switch": switch}
    return tracer
