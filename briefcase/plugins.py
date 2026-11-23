"""Plugin entry points: let another package add to briefcase without editing it.

A package declares entry points in its ``pyproject.toml``; briefcase finds them with
:mod:`importlib.metadata` and feeds them into the registry that already exists for each
kind of extension::

    [project.entry-points."briefcase.exporters"]
    acme = "acme_briefcase.exporter:AcmeExporter"      # briefcase.observe("acme:...")

Groups (``GROUPS`` below) and what each entry point must name:

``briefcase.exporters``
    A :class:`~briefcase.exporters.base.BaseExporter` subclass. ``briefcase.observe("NAME")``,
    ``briefcase.observe("NAME:TARGET")`` and ``BRIEFCASE_EXPORTER=NAME:TARGET`` build it with
    ``cls.from_target(TARGET)`` when the class has that classmethod, else ``cls(TARGET)``, or
    ``cls()`` when there is no target.
``briefcase.bitemporal_backends``
    A class implementing :class:`~briefcase.bitemporal.BitemporalStore`;
    ``briefcase.bitemporal.backends.get_backend(NAME)`` returns it.
``briefcase.vcs_providers``
    A :class:`~briefcase.integrations.vcs.VcsClientBase` subclass;
    ``briefcase.integrations.vcs.get_provider(NAME)`` returns it.
``briefcase.receiver_sources``
    A callable ``normalize(payload: dict) -> event | list[event] | None``. The receiver
    accepts ``POST /v1/sources/NAME`` (JSON) and maps the events like its built-in ones.
``briefcase.connect_adapters``
    A :class:`briefcase.connect.adapters.Adapter` subclass with a ``DATA`` dict (the same
    keys as an entry of ``adapters.json``); ``briefcase-ai connect NAME`` plans for it.
``briefcase.guardrails``
    A guardrail environment class; ``briefcase.guardrails.framework.make(NAME)`` builds it.
``briefcase.cli_commands``
    A module with ``register(subparsers)`` (the protocol of ``briefcase/cli/*_commands.py``)
    that adds the command ``NAME``. It is imported only when ``briefcase-ai NAME`` runs.
``briefcase.features``
    A feature manifest (a dict, JSON text, or a callable returning a dict) that
    ``briefcase-ai features`` lists.

Contract:

- Lazy: nothing is imported until its group and name are needed. Listing entry points reads
  package metadata only. ``briefcase-ai --help`` imports no plugin.
- Isolated: a plugin that raises on import, names something missing, or fails its group's
  check is skipped with one WARNING on the ``briefcase.plugins`` logger naming the
  distribution. It never breaks the CLI or the application.
- Deterministic: entry points are ordered by ``(group, name, distribution)``. Two
  distributions offering one name: the alphabetically first distribution wins and the
  other is skipped with a warning. A built-in name always wins over a plugin (warning).
- Switches: ``BRIEFCASE_PLUGINS=off`` loads no plugin at all;
  ``BRIEFCASE_PLUGINS_DISABLE=name,group:name,distribution`` skips the ones named.
- API version: a plugin may declare ``BRIEFCASE_PLUGIN_API = 1`` on the loaded object or in
  its module. A version this release does not support is refused with a clear message;
  no declaration is read as version 1.

``briefcase-ai plugins`` and ``briefcase-ai doctor`` report every entry point and what became of it.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from briefcase._logging import get_logger

logger = get_logger(__name__)

#: The plugin API this release implements. A plugin declaring a higher number needs a newer
#: briefcase-ai; one declaring a number below ``MIN_PLUGIN_API`` is too old for this one.
PLUGIN_API_VERSION = 1
MIN_PLUGIN_API = 1
API_ATTRIBUTE = "BRIEFCASE_PLUGIN_API"

ENV_SWITCH = "BRIEFCASE_PLUGINS"
ENV_DISABLE = "BRIEFCASE_PLUGINS_DISABLE"

PREFIX = "briefcase."
GROUPS: Dict[str, str] = {
    "briefcase.exporters": "exporter class for briefcase.observe('NAME:...') and BRIEFCASE_EXPORTER",
    "briefcase.bitemporal_backends": "bitemporal store backend class",
    "briefcase.vcs_providers": "data-versioning client class, a VcsClientBase",
    "briefcase.receiver_sources": "normaliser for POST /v1/sources/NAME on the receiver",
    "briefcase.connect_adapters": "adapter class for `briefcase-ai connect NAME`",
    "briefcase.guardrails": "guardrail environment class for guardrails.make('NAME')",
    "briefcase.cli_commands": "module whose register(subparsers) adds `briefcase-ai NAME`",
    "briefcase.features": "feature manifest listed by `briefcase-ai features`",
}

_NOUNS = {
    "briefcase.exporters": "exporter", "briefcase.bitemporal_backends": "backend",
    "briefcase.vcs_providers": "provider", "briefcase.receiver_sources": "source",
    "briefcase.connect_adapters": "adapter", "briefcase.guardrails": "guardrail",
    "briefcase.cli_commands": "command", "briefcase.features": "feature",
}

# States an entry point can be in.
AVAILABLE = "available"   # found, not imported yet
LOADED = "loaded"
SKIPPED = "skipped"       # import failed, wrong type, or another distribution has the name
REFUSED = "refused"       # declared an unsupported plugin API version
DISABLED = "disabled"     # BRIEFCASE_PLUGINS=off or BRIEFCASE_PLUGINS_DISABLE
SHADOWED = "shadowed"     # a built-in has the same name; the built-in is used

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,99}$")


@dataclass
class Plugin:
    """One entry point and what became of it."""

    group: str
    name: str
    value: str                      # "module:attr"
    dist: Optional[str] = None
    version: Optional[str] = None
    state: str = AVAILABLE
    reason: Optional[str] = None
    api: Optional[int] = None
    obj: Any = field(default=None, repr=False, compare=False)
    _ep: Any = field(default=None, repr=False, compare=False)

    @property
    def key(self) -> str:
        return f"{self.group}:{self.name}"

    @property
    def short_group(self) -> str:
        return self.group[len(PREFIX):] if self.group.startswith(PREFIX) else self.group

    def to_dict(self) -> Dict[str, Any]:
        return {
            "group": self.group, "name": self.name, "value": self.value,
            "distribution": self.dist, "version": self.version, "state": self.state,
            "reason": self.reason, "api": self.api,
        }


_lock = threading.RLock()
_cache: Optional[List[Plugin]] = None
_warned: set = set()


def _one_line(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _env(env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    return dict(os.environ) if env is None else env


def enabled(env: Optional[Dict[str, str]] = None) -> bool:
    """False when ``BRIEFCASE_PLUGINS`` turns plugins off (off, 0, false, no, none)."""
    value = (_env(env).get(ENV_SWITCH) or "").strip().lower()
    return value not in ("off", "0", "false", "no", "none")


def disabled_names(env: Optional[Dict[str, str]] = None) -> List[str]:
    raw = _env(env).get(ENV_DISABLE) or ""
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def _is_disabled(plugin: Plugin, names: Sequence[str]) -> Optional[str]:
    candidates = {plugin.name.lower(), plugin.key.lower(), f"{plugin.short_group}:{plugin.name}".lower()}
    if plugin.dist:
        candidates.add(plugin.dist.lower())
        candidates.add(_normalize(plugin.dist))
    for name in names:
        if name in candidates or _normalize(name) in candidates:
            return f"named in {ENV_DISABLE}"
    return None


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _any_declared() -> bool:
    """Quick check before importing :mod:`importlib.metadata` (which costs several ms): does
    any ``entry_points.txt`` on ``sys.path`` mention a ``briefcase.`` group? True whenever it
    cannot tell (a zip or unreadable path entry), so the full scan then decides."""
    for entry in sys.path:
        directory = entry or "."
        try:
            if not os.path.isdir(directory):
                if os.path.exists(directory):
                    return True  # a zip or egg file: let importlib.metadata read it
                continue
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if not name.endswith((".dist-info", ".egg-info")):
                continue
            try:
                with open(os.path.join(directory, name, "entry_points.txt"), "rb") as fh:
                    if b"[briefcase." in fh.read():
                        return True
            except OSError:
                continue
    return False


def _raw_entry_points() -> List[Tuple[str, str, str, Optional[str], Optional[str], Any]]:
    """``(group, name, value, dist, version, entry_point)`` for every ``briefcase.*`` group,
    from package metadata only (nothing is imported)."""
    if not _any_declared():
        return []
    from importlib import metadata

    out: List[Tuple[str, str, str, Optional[str], Optional[str], Any]] = []
    if sys.version_info >= (3, 10):
        try:
            everything = metadata.entry_points()
        except Exception as exc:  # noqa: BLE001 - a broken metadata file must not break us
            logger.warning("could not read package entry points: %s", _one_line(exc))
            return []
        seen: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        for group in GROUPS:
            for ep in everything.select(group=group):
                dist = getattr(ep, "dist", None)
                name = version = None
                if dist is not None:
                    key = str(getattr(dist, "_path", id(dist)))
                    if key not in seen:  # one METADATA parse per distribution
                        try:
                            seen[key] = (dist.metadata["Name"], dist.version)
                        except Exception:  # noqa: BLE001
                            seen[key] = (getattr(dist, "_normalized_name", None), None)
                    name, version = seen[key]
                out.append((group, ep.name, ep.value, name, version, ep))
        return out
    seen = set()  # pragma: no cover - Python 3.9
    for dist in metadata.distributions():  # pragma: no cover
        try:
            eps = [ep for ep in dist.entry_points if ep.group in GROUPS]
            if not eps:
                continue
            name = dist.metadata["Name"]
        except Exception:  # noqa: BLE001
            continue
        if _normalize(name or "") in seen:
            continue
        seen.add(_normalize(name or ""))
        for ep in eps:
            out.append((ep.group, ep.name, ep.value, name, dist.version, ep))
    return out


def _discover(env: Optional[Dict[str, str]] = None) -> List[Plugin]:
    names = disabled_names(env)
    switched_off = not enabled(env)
    found = [Plugin(group=g, name=n, value=v, dist=d, version=ver, _ep=ep)
             for g, n, v, d, ver, ep in _raw_entry_points()]
    found.sort(key=lambda p: (p.group, p.name, _normalize(p.dist or ""), p.value))
    winners: Dict[str, Plugin] = {}
    for plugin in found:
        if not _NAME.match(plugin.name):
            plugin.state, plugin.reason = SKIPPED, f"{plugin.name!r} is not a valid plugin name"
        elif switched_off:
            plugin.state, plugin.reason = DISABLED, f"{ENV_SWITCH}=off"
        else:
            why = _is_disabled(plugin, names)
            if why:
                plugin.state, plugin.reason = DISABLED, why
        if plugin.state != AVAILABLE:
            continue
        first = winners.get(plugin.key)
        if first is not None:
            plugin.state = SKIPPED
            plugin.reason = (f"name {plugin.name!r} in {plugin.group} is also provided by "
                             f"{first.dist}, which is used (alphabetical order of distributions)")
            _warn_once(plugin, plugin.reason)
            continue
        winners[plugin.key] = plugin
    return found


def all_plugins(env: Optional[Dict[str, str]] = None) -> List[Plugin]:
    """Every ``briefcase.*`` entry point, in deterministic order, from metadata only.
    Cached for the process unless ``env`` is given; :func:`clear_cache` resets it."""
    global _cache
    if env is not None:
        return _discover(env)
    with _lock:
        if _cache is None:
            _cache = _discover()
        return _cache


def clear_cache() -> None:
    """Forget what was discovered and loaded (tests, or after installing a package)."""
    global _cache
    with _lock:
        _cache = None
        _warned.clear()


def entries(group: str) -> List[Plugin]:
    """The entry points of one group (no import)."""
    _check_group(group)
    return [plugin for plugin in all_plugins() if plugin.group == group]


def find(group: str, name: str) -> Optional[Plugin]:
    """The entry point that would be used for ``name`` in ``group``, or the first one that
    was skipped or disabled when none would be; ``None`` when nothing has that name."""
    candidates = [p for p in entries(group) if p.name == name]
    for plugin in candidates:
        if plugin.state in (AVAILABLE, LOADED, REFUSED, SHADOWED):
            return plugin
    return candidates[0] if candidates else None


def names(group: str) -> List[str]:
    """Names in ``group`` that could be used (not disabled or skipped as duplicates)."""
    out: List[str] = []
    for plugin in entries(group):
        if plugin.state in (AVAILABLE, LOADED) and plugin.name not in out:
            out.append(plugin.name)
    return out


def _check_group(group: str) -> None:
    if group not in GROUPS:
        raise ValueError(f"unknown plugin group {group!r}; one of: {', '.join(GROUPS)}")


def _warn_once(plugin: Plugin, message: str) -> None:
    key = (plugin.key, plugin.dist, message)
    if key in _warned:
        return
    _warned.add(key)
    who = f"{plugin.dist} {plugin.version}" if plugin.dist else "an unknown distribution"
    logger.warning("briefcase plugin %s from %s: %s", plugin.key, who, message)


def _declared_api(obj: Any) -> Tuple[Optional[int], Optional[str]]:
    """``(version, problem)`` from ``BRIEFCASE_PLUGIN_API`` on ``obj`` or its module."""
    holders = [obj]
    module_name = getattr(obj, "__module__", None)
    if isinstance(module_name, str) and module_name in sys.modules:
        holders.append(sys.modules[module_name])
    for holder in holders:
        if hasattr(holder, API_ATTRIBUTE):
            value = getattr(holder, API_ATTRIBUTE)
            if isinstance(value, int) and not isinstance(value, bool):
                return value, None
            return None, f"{API_ATTRIBUTE} must be an integer, got {value!r}"
    return None, None


def check_api(obj: Any) -> Tuple[Optional[int], Optional[str]]:
    """``(declared version, refusal message or None)``."""
    version, problem = _declared_api(obj)
    if problem:
        return version, problem
    if version is None:
        return None, None
    if version > PLUGIN_API_VERSION:
        return version, (f"needs briefcase plugin API {version}, but this briefcase-ai supports "
                         f"{MIN_PLUGIN_API}..{PLUGIN_API_VERSION}; upgrade with: "
                         "pip install -U briefcase-ai")
    if version < MIN_PLUGIN_API:
        return version, (f"written for briefcase plugin API {version}, which this briefcase-ai "
                         f"no longer supports (needs {MIN_PLUGIN_API}..{PLUGIN_API_VERSION}); "
                         "upgrade the plugin")
    return version, None


Validator = Callable[[Any], Optional[str]]


def load_plugin(plugin: Plugin, validate: Optional[Validator] = None) -> Any:
    """Import one entry point. Returns the object, or ``None`` with ``plugin.state`` saying
    why (skipped / refused / disabled / shadowed). Never raises."""
    with _lock:
        if plugin.state == LOADED:
            return plugin.obj
        if plugin.state != AVAILABLE:
            return None
        try:
            obj = plugin._ep.load() if plugin._ep is not None else _load_value(plugin.value)
        except BaseException as exc:  # noqa: BLE001 - SystemExit in a plugin must not end the host
            if isinstance(exc, KeyboardInterrupt):
                raise
            plugin.state, plugin.reason = SKIPPED, f"import failed: {_one_line(exc)}"
            _warn_once(plugin, plugin.reason)
            return None
        version, refusal = check_api(obj)
        plugin.api = version
        if refusal:
            plugin.state, plugin.reason = REFUSED, refusal
            _warn_once(plugin, f"refused: {refusal}")
            return None
        if validate is not None:
            try:
                problem = validate(obj)
            except Exception as exc:  # noqa: BLE001
                problem = f"check failed: {_one_line(exc)}"
            if problem:
                plugin.state, plugin.reason = SKIPPED, problem
                _warn_once(plugin, plugin.reason)
                return None
        plugin.state, plugin.obj = LOADED, obj
        plugin.reason = None if version is not None else f"no {API_ATTRIBUTE} declared; read as 1"
        return obj


def _load_value(value: str) -> Any:
    import importlib

    module_name, _, attr = value.partition(":")
    obj = importlib.import_module(module_name.strip())
    for part in filter(None, attr.strip().split(".")):
        obj = getattr(obj, part)
    return obj


def shadow(group: str, builtin_names: Iterable[str]) -> None:
    """Mark plugins whose name a built-in already uses: the built-in wins (warning)."""
    taken = set(builtin_names)
    for plugin in entries(group):
        if plugin.name in taken and plugin.state == AVAILABLE:
            plugin.state = SHADOWED
            plugin.reason = f"a built-in {_NOUNS[group]} named {plugin.name!r} is used instead"
            _warn_once(plugin, plugin.reason)


def load(group: str, name: str, *, builtin_names: Iterable[str] = (),
         validate: Optional[Validator] = None) -> Any:
    """The object behind ``name`` in ``group``, imported now; ``None`` when there is no such
    plugin or it could not be used (see :func:`find` for why)."""
    shadow(group, builtin_names)
    plugin = find(group, name)
    if plugin is None:
        return None
    return load_plugin(plugin, validate)


def load_all(group: str, *, builtin_names: Iterable[str] = (),
             validate: Optional[Validator] = None) -> Dict[str, Any]:
    """``{name: object}`` for every usable plugin in ``group`` (imports them all)."""
    shadow(group, builtin_names)
    out: Dict[str, Any] = {}
    for plugin in entries(group):
        if plugin.name in out:
            continue
        obj = load_plugin(plugin, validate)
        if obj is not None:
            out[plugin.name] = obj
    return out


def explain(group: str, name: str) -> str:
    """One line for an error message: why ``name`` in ``group`` is not usable."""
    plugin = find(group, name)
    if plugin is None:
        return f"no installed package provides {name!r} in {group}"
    who = f"{plugin.dist} {plugin.version}" if plugin.dist else plugin.value
    return f"{plugin.key} from {who} is {plugin.state}" + (f": {plugin.reason}" if plugin.reason else "")


# -- checks per group (used by the hooks in each registry and by `briefcase-ai plugins`) -----

def _subclass_of(base_path: str, what: str) -> Validator:
    def check(obj: Any) -> Optional[str]:
        import importlib

        module_name, _, attr = base_path.partition(":")
        base = getattr(importlib.import_module(module_name), attr)
        if not (isinstance(obj, type) and issubclass(obj, base)):
            return f"{_describe(obj)} is not a {what} subclass"
        return None
    return check


def _describe(obj: Any) -> str:
    return getattr(obj, "__qualname__", None) or type(obj).__name__


def _is_class(what: str) -> Validator:
    def check(obj: Any) -> Optional[str]:
        return None if isinstance(obj, type) else f"{_describe(obj)} is not a class ({what})"
    return check


def _callable(obj: Any) -> Optional[str]:
    return None if callable(obj) else f"{_describe(obj)} is not callable"


def _command_module(obj: Any) -> Optional[str]:
    return None if callable(getattr(obj, "register", None)) else "has no register(subparsers) function"


def _connect_adapter(obj: Any) -> Optional[str]:
    problem = _subclass_of("briefcase.connect.adapters:Adapter", "briefcase.connect.adapters.Adapter")(obj)
    if problem:
        return problem
    data = getattr(obj, "DATA", None)
    missing = [key for key in ("name", "mode", "docs_url", "verified_on")
               if not isinstance(data, dict) or not data.get(key)]
    if missing:
        return f"{obj.__name__}.DATA lacks {', '.join(missing)}"
    return None


def _feature(obj: Any) -> Optional[str]:
    return None if isinstance(obj, (dict, str)) or callable(obj) else \
        "is not a manifest (a dict, JSON text, or a callable returning one)"


VALIDATORS: Dict[str, Validator] = {
    "briefcase.exporters": _subclass_of("briefcase.exporters.base:BaseExporter", "BaseExporter"),
    "briefcase.bitemporal_backends": _is_class("bitemporal backend"),
    "briefcase.vcs_providers": _subclass_of("briefcase.integrations.vcs.base:VcsClientBase", "VcsClientBase"),
    "briefcase.receiver_sources": _callable,
    "briefcase.connect_adapters": _connect_adapter,
    "briefcase.guardrails": _is_class("guardrail environment"),
    "briefcase.cli_commands": _command_module,
    "briefcase.features": _feature,
}


def get(group: str, name: str, builtin_names: Iterable[str] = ()) -> Any:
    """:func:`load` with the group's own check."""
    return load(group, name, builtin_names=builtin_names, validate=VALIDATORS.get(group))


def get_all(group: str, builtin_names: Iterable[str] = ()) -> Dict[str, Any]:
    """:func:`load_all` with the group's own check."""
    return load_all(group, builtin_names=builtin_names, validate=VALIDATORS.get(group))


def builtin_names(group: str) -> List[str]:
    """Names the SDK itself uses in ``group`` (a plugin with one of these is shadowed)."""
    try:
        if group == "briefcase.exporters":
            from briefcase._observe import BUILTIN_EXPORTER_NAMES

            return list(BUILTIN_EXPORTER_NAMES)
        if group == "briefcase.bitemporal_backends":
            from briefcase.bitemporal.backends import BUILTIN_BACKENDS

            return list(BUILTIN_BACKENDS)
        if group == "briefcase.vcs_providers":
            from briefcase.integrations.vcs import BUILTIN_PROVIDERS

            return list(BUILTIN_PROVIDERS)
        if group == "briefcase.receiver_sources":
            from briefcase.receiver.server import BUILTIN_SOURCES

            return list(BUILTIN_SOURCES)
        if group == "briefcase.connect_adapters":
            from briefcase.connect.adapters import builtin_ids

            return builtin_ids()
        if group == "briefcase.guardrails":
            from briefcase.guardrails.framework.registry import builtin_ids as guardrail_ids

            return guardrail_ids()
        if group == "briefcase.cli_commands":
            from briefcase.cli import _discovery

            return _discovery.builtin_command_names()
        if group == "briefcase.features":
            from briefcase.features import load_catalog

            return [feature.id for feature in load_catalog(plugins=False).features]
    except Exception as exc:  # noqa: BLE001 - a report must still print
        logger.debug("built-in names for %s unavailable: %s", group, _one_line(exc))
    return []


def report(load_them: bool = True) -> List[Plugin]:
    """Every entry point, each loaded (with its group's check) when ``load_them`` is true, so
    its state says whether it works. Used by ``briefcase-ai plugins`` and ``briefcase-ai doctor``."""
    plugins = all_plugins()
    if load_them:
        for group in GROUPS:
            get_all(group, builtin_names(group))
    return plugins


__all__ = [
    "API_ATTRIBUTE", "ENV_DISABLE", "ENV_SWITCH", "GROUPS", "MIN_PLUGIN_API", "PLUGIN_API_VERSION",
    "Plugin", "all_plugins", "check_api", "clear_cache", "enabled", "entries", "explain", "find",
    "get", "get_all", "load", "load_all", "load_plugin", "names", "report",
]
