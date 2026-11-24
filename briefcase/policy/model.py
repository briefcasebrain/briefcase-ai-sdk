"""The policy file: locations, parsing, validation and layer merging.

A policy is read from up to three files, strongest first:

1. system  ``/etc/briefcase/policy.toml`` (deployed by an administrator)
2. user    ``~/.briefcase/policy.toml``
3. project ``<project>/.briefcase/policy.toml``

The strongest file that exists is the *base*: its rules and settings apply as
written. Every weaker file is *restrict-only*: it can add ``ask``/``deny``
rules and move a setting to a stricter value, and nothing else. Its ``allow``
rules, its ``disable`` list and any setting that would loosen the base are
ignored and reported by ``briefcase-ai policy check``.

A file is applied whole or not at all. One unknown key, one bad pattern, and
the file is rejected: a typo must never quietly switch a rule off.

Standard library only, and no ``dataclasses``: this module is imported by the
hook on every tool call.
"""

from __future__ import annotations

import os
import stat
import sys

VERSION = 1
EFFECTS = ("allow", "ask", "deny")
RANK = {"allow": 0, "ask": 1, "deny": 2}
PRESETS = ("observe", "guarded", "strict")
PRESET_RANK = {"observe": 0, "guarded": 1, "strict": 2}
MODES = ("observe", "enforce")
TOOL_KINDS = ("shell", "read", "write", "mcp", "web")
# ``tool = "file"`` is shorthand for read + write.
TOOL_ALIASES = {"file": ("read", "write"), "edit": ("write",), "fetch": ("web",)}

DEFAULT_BRANCHES = ["main", "master", "trunk", "production", "prod", "release", "release/*"]

# setting -> (validator, how a restrict-only layer may change it)
_BOOL, _EFFECT, _FALLBACK = "bool", "effect", "fallback"
SETTINGS = {
    "version": "int",
    "preset": "preset",
    "mode": "mode",
    "fail_closed": _BOOL,
    "on_unparseable": _EFFECT,
    "default": _EFFECT,
    "ask_fallback": _FALLBACK,
    "protect_self": _BOOL,
    "protected_branches": "strlist",
    "disable": "strlist",
}
RULE_KEYS = {
    "id": "str", "tool": "tools", "effect": "effect", "reason": "str",
    "command": "strlist", "paths": "strlist", "except": "strlist", "outside": "strlist",
    "server": "strlist", "name": "strlist", "hosts": "strlist",
}
_MATCHERS_BY_KIND = {
    "shell": ("command",), "read": ("paths", "except", "outside"), "write": ("paths", "except", "outside"),
    "mcp": ("server", "name"), "web": ("hosts",),
}


class PolicyError(Exception):
    """A policy file that cannot be applied. ``path`` is the offending file."""

    def __init__(self, message, path=None):
        Exception.__init__(self, message)
        self.path = path


# --- locations ----------------------------------------------------------------

def briefcase_home():
    override = os.environ.get("BRIEFCASE_HOME")
    return os.path.expanduser(override) if override else os.path.join(os.path.expanduser("~"), ".briefcase")


def user_policy_path():
    return os.path.join(briefcase_home(), "policy.toml")


def system_policy_path():
    """Where an administrator deploys a policy. ``BRIEFCASE_SYSTEM_POLICY``
    overrides it (tests, non-standard layouts)."""
    override = os.environ.get("BRIEFCASE_SYSTEM_POLICY")
    if override is not None:
        return override
    if sys.platform == "darwin":
        return "/Library/Application Support/Briefcase/policy.toml"
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Briefcase", "policy.toml")
    return "/etc/briefcase/policy.toml"


def state_dir():
    """Decision log, health file and hash key: ``~/.briefcase/policy/``."""
    return os.path.join(briefcase_home(), "policy")


def find_project_policy(start):
    """``.briefcase/policy.toml`` in ``start`` or the nearest ancestor.

    The search stops at the home directory and never returns the user policy
    itself, so ``~/.briefcase/policy.toml`` cannot count twice."""
    if not start:
        return None
    user = os.path.realpath(user_policy_path())
    home = os.path.realpath(os.path.expanduser("~"))
    current = os.path.realpath(start)
    for _ in range(64):
        candidate = os.path.join(current, ".briefcase", "policy.toml")
        try:
            exists = stat.S_ISREG(os.stat(candidate).st_mode)
        except (FileNotFoundError, NotADirectoryError):
            exists = False
        except OSError:
            # Permission denied (or anything else) on the way to it: there may be a restriction in there. Report it
            # as a policy that cannot be read instead of walking past it to a weaker one.
            if os.path.realpath(candidate) != user:
                return candidate
            exists = False
        if exists and os.path.realpath(candidate) != user:
            return candidate
        parent = os.path.dirname(current)
        if parent == current or current == home:
            return None
        current = parent
    return None


# --- parsing ------------------------------------------------------------------

def _toml_module():
    """``tomllib`` (Python 3.11+), else ``tomli`` when installed, else ``None``."""
    import importlib
    for name in ("tomllib", "tomli"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return None


def parse_toml(text, path=None):
    toml = _toml_module()
    if toml is None:
        raise PolicyError("no TOML parser on this Python; use Python 3.11+ or run: pip install tomli", path)
    try:
        return toml.loads(text)
    except Exception as exc:  # tomllib.TOMLDecodeError, and tomli's own class
        raise PolicyError(f"not valid TOML: {exc}", path)


def _strlist(value, what, path):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise PolicyError(f"{what} must be a string or a list of strings", path)


def _shown(value):
    """A value from a policy file as it may appear in an error: short, and never a whole document."""
    text = repr(value)
    return text if len(text) <= 24 else text[:21] + "..."


def _check(kind, value, what, path):
    if kind == _BOOL:
        if not isinstance(value, bool):
            raise PolicyError(f"{what} must be true or false", path)
        return value
    if kind == "int":
        if value != VERSION or isinstance(value, bool):
            raise PolicyError(f"{what} must be {VERSION}; this briefcase understands no other policy version", path)
        return value
    if kind == "str":
        if not isinstance(value, str) or not value.strip():
            raise PolicyError(f"{what} must be a non-empty string", path)
        return value
    if kind == "strlist":
        return _strlist(value, what, path)
    choices = {"preset": PRESETS, "mode": MODES, _EFFECT: EFFECTS, _FALLBACK: ("allow", "deny")}.get(kind)
    if choices is not None:
        if value not in choices:
            raise PolicyError(f"{what} must be one of: {', '.join(choices)} (got {_shown(value)})", path)
        return value
    if kind == "tools":
        kinds = []
        for name in _strlist(value, what, path):
            expanded = TOOL_ALIASES.get(name, (name,))
            for item in expanded:
                if item not in TOOL_KINDS:
                    raise PolicyError(
                        f"{what}: unknown tool {_shown(name)}; use one of: shell, read, write, file, mcp, web", path)
                if item not in kinds:
                    kinds.append(item)
        if not kinds:
            raise PolicyError(f"{what} must name at least one tool", path)
        return kinds
    raise AssertionError(kind)  # pragma: no cover - table error


class Rule(object):
    """One rule, already validated. ``check`` names a built-in predicate and
    is only ever set by :mod:`briefcase.policy.presets`."""

    __slots__ = ("id", "kinds", "effect", "reason", "command", "paths", "excepts", "outside",
                 "server", "name", "hosts", "source", "check", "_compiled")

    def __init__(self, id, kinds, effect, reason="", command=(), paths=(), excepts=(), outside=(),
                 server=(), name=(), hosts=(), source="user", check=None):
        self.id = id
        self.kinds = tuple(kinds)
        self.effect = effect
        self.reason = reason
        self.command = tuple(command)
        self.paths = tuple(paths)
        self.excepts = tuple(excepts)
        self.outside = tuple(outside)
        self.server = tuple(server)
        self.name = tuple(name)
        self.hosts = tuple(hosts)
        self.source = source
        self.check = check
        self._compiled = None

    def describe(self):
        """The matcher in the words of the policy file, for ``policy check``."""
        parts = []
        for label, values in (("command", self.command), ("paths", self.paths), ("except", self.excepts),
                              ("outside", self.outside), ("server", self.server), ("name", self.name),
                              ("hosts", self.hosts)):
            if values:
                parts.append(f"{label}={', '.join(values)}")
        if self.check:
            parts.append(f"built-in check: {self.check}")
        return "; ".join(parts) or "every call"

    def to_dict(self):
        out = {"id": self.id, "tool": list(self.kinds), "effect": self.effect, "source": self.source}
        if self.reason:
            out["reason"] = self.reason
        for key, values in (("command", self.command), ("paths", self.paths), ("except", self.excepts),
                            ("outside", self.outside), ("server", self.server), ("name", self.name),
                            ("hosts", self.hosts)):
            if values:
                out[key] = list(values)
        if self.check:
            out["check"] = self.check
        return out


def build_rule(raw, index, source, path=None):
    if not isinstance(raw, dict):
        raise PolicyError(f"rule #{index} must be a table ([[rule]])", path)
    label = f"rule #{index}" + (f" ({raw['id']})" if isinstance(raw.get("id"), str) else "")
    unknown = sorted(set(raw) - set(RULE_KEYS))
    if unknown:
        known = ", ".join(sorted(RULE_KEYS))
        raise PolicyError(f"{label}: unknown key {_shown(unknown[0])}; known keys: {known}", path)
    for required in ("tool", "effect"):
        if required not in raw:
            raise PolicyError(f"{label}: missing `{required}`", path)
    values = {key: _check(RULE_KEYS[key], raw[key], f"{label}: `{key}`", path) for key in raw}
    kinds = values["tool"]
    allowed = set()
    for kind in kinds:
        allowed.update(_MATCHERS_BY_KIND[kind])
    for key in ("command", "paths", "except", "outside", "server", "name", "hosts"):
        if key in values and key not in allowed:
            raise PolicyError(f"{label}: `{key}` does not apply to tool {'/'.join(kinds)}", path)
    if len(kinds) > 1 and set(kinds) - {"read", "write"}:
        raise PolicyError(f"{label}: only read and write can share one rule; write one rule per tool", path)
    if "command" in values:
        from briefcase.policy import shell
        for pattern in values["command"]:
            try:
                shell.compile_pattern(pattern)
            except ValueError as exc:
                raise PolicyError(f"{label}: command pattern {_shown(pattern)}: {exc}", path)
    rule_id = values.get("id") or f"{source}.{index}"
    if not all(ch.isalnum() or ch in "._-" for ch in rule_id):
        raise PolicyError(f"{label}: `id` may only use letters, digits, '.', '_' and '-'", path)
    return Rule(
        rule_id, kinds, values["effect"], values.get("reason", ""), values.get("command", ()),
        values.get("paths", ()), values.get("except", ()), values.get("outside", ()),
        values.get("server", ()), values.get("name", ()), values.get("hosts", ()), source=source,
    )


class Layer(object):
    """One validated policy file."""

    __slots__ = ("source", "path", "settings", "rules")

    def __init__(self, source, path, settings, rules):
        self.source = source
        self.path = path
        self.settings = settings
        self.rules = rules


def parse_layer(text, source, path=None):
    """Validate one file completely; raises :class:`PolicyError` on the first problem."""
    doc = parse_toml(text, path)
    known = set(SETTINGS) | {"rule"}
    unknown = sorted(set(doc) - known)
    if unknown:
        hint = " (rules are written as [[rule]])" if unknown[0] in ("rules", "Rule") else ""
        raise PolicyError(f"unknown key {_shown(unknown[0])}{hint}; known keys: {', '.join(sorted(known))}", path)
    if "version" not in doc:
        raise PolicyError(f"missing `version = {VERSION}`", path)
    settings = {key: _check(SETTINGS[key], doc[key], f"`{key}`", path) for key in doc if key != "rule"}
    raw_rules = doc.get("rule", [])
    if not isinstance(raw_rules, list):
        raise PolicyError("`rule` must be an array of tables: write [[rule]]", path)
    rules = [build_rule(raw, index, source, path) for index, raw in enumerate(raw_rules, 1)]
    seen = set()
    for rule in rules:
        if rule.id in seen:
            raise PolicyError(f"two rules share the id {rule.id!r}", path)
        seen.add(rule.id)
    return Layer(source, path, settings, rules)


def load_layer(path, source):
    """``Layer`` for an existing file, ``None`` when there is no file."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise PolicyError(f"cannot read: {exc}", path)
    return parse_layer(text, source, path)


def declares_fail_closed(path, text=None):
    """Does a file that may not even parse say ``fail_closed = true``?

    A syntax error elsewhere in the file must not turn a fail-closed policy
    into a fail-open one, so the hook falls back to this line scan when the
    file is rejected."""
    try:
        if text is None:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
    except OSError:
        return False
    for line in text.splitlines():
        compact = line.split("#", 1)[0].replace(" ", "").replace("\t", "").strip().lower()
        if compact == "fail_closed=true":
            return True
    return False


# --- merging ------------------------------------------------------------------

def _defaults(preset):
    return {
        "preset": preset,
        "mode": "observe" if preset == "observe" else "enforce",
        "fail_closed": False,
        "on_unparseable": {"observe": "allow", "guarded": "ask", "strict": "deny"}[preset],
        "default": "allow",
        "ask_fallback": "deny",
        "protect_self": preset != "observe",
        "protected_branches": list(DEFAULT_BRANCHES),
    }


class Policy(object):
    """The merged policy the engine evaluates.

    ``groups`` is a list of rule lists. Inside a group the first matching rule
    decides; across groups the most restrictive result wins. Group 0 is the
    base file's rules followed by the preset's; every restrict-only file is a
    group of its own, so nothing in it can pre-empt a base rule.
    """

    __slots__ = ("settings", "groups", "protect", "layers", "notes", "disabled", "default_declared")

    def __init__(self, settings, groups, protect, layers, notes, disabled, default_declared=False):
        self.default_declared = default_declared  # does a file state `default` itself, or is it the built-in one?
        self.settings = settings
        self.groups = groups
        self.protect = protect
        self.layers = layers
        self.notes = notes
        self.disabled = disabled

    def __getattr__(self, name):
        try:
            return self.settings[name]
        except KeyError:
            raise AttributeError(name)

    def all_rules(self):
        return [rule for group in self.groups for rule in group]


def merge(layers):
    """Combine validated layers (strongest first) into a :class:`Policy`."""
    from briefcase.policy import presets

    notes = []
    if not layers:
        return Policy(_defaults("observe"), [[]], [], [], notes, [])
    base = layers[0]
    preset = base.settings.get("preset", "guarded")
    settings = _defaults(preset)
    for key, value in base.settings.items():
        if key not in ("version", "disable"):
            settings[key] = value
    stricter = []
    for layer in layers[1:]:
        where = f"{layer.source} policy ({layer.path})"
        for key, value in layer.settings.items():
            if key in ("version",):
                continue
            if key == "disable":
                notes.append(f"{where}: `disable` is ignored; only the {base.source} policy can switch rules off")
                continue
            if key == "protected_branches":
                settings[key] = settings[key] + [b for b in value if b not in settings[key]]
                continue
            current = settings[key]
            if key == "preset":
                tighter = PRESET_RANK[value] > PRESET_RANK[current]
            elif key == "mode":
                tighter = value == "enforce" and current == "observe"
            elif key in ("fail_closed", "protect_self"):
                tighter = value and not current
            else:  # on_unparseable, default, ask_fallback
                tighter = RANK[value] > RANK[current]
            if tighter:
                settings[key] = value
            elif value != current:
                notes.append(f"{where}: `{key} = {_show(value)}` is ignored; it would loosen "
                             f"`{key} = {_show(current)}` from the {base.source} policy")
        kept = []
        for rule in layer.rules:
            if rule.effect == "allow":
                notes.append(f"{where}: rule {rule.id!r} is ignored; a {layer.source} policy can add "
                             "ask and deny rules but cannot allow")
            else:
                kept.append(rule)
        stricter.append(kept)
    # Preset rules are built last, from the final settings, so a stricter
    # preset or more protected branches in a weaker file take effect.
    disabled = list(base.settings.get("disable", []))
    preset_rules = presets.rules(settings["preset"], settings)
    known = {rule.id for rule in preset_rules}
    for rule_id in disabled:
        if rule_id not in known:
            notes.append(f"{base.source} policy ({base.path}): `disable` names {rule_id!r}, which is not a "
                         f"rule of the {settings['preset']} preset")
    preset_rules = [rule for rule in preset_rules if rule.id not in disabled]
    protect = presets.protection(settings) if settings["protect_self"] else []
    groups = [list(base.rules) + preset_rules] + stricter
    return Policy(settings, groups, protect, layers, notes, disabled, "default" in base.settings)


def _show(value):
    return str(value).lower() if isinstance(value, bool) else repr(value).replace("'", '"')


class Loaded(object):
    """What :func:`load` found: the policy, or the reason there is none."""

    __slots__ = ("policy", "errors", "paths", "fail_closed")

    def __init__(self, policy, errors, paths, fail_closed):
        self.policy = policy
        self.errors = errors
        self.paths = paths
        self.fail_closed = fail_closed


def layer_paths(project_dir=None):
    """``[(source, path)]`` for every policy file that could apply, strongest first."""
    out = [("system", system_policy_path()), ("user", user_policy_path())]
    project = find_project_policy(project_dir)
    if project:
        out.append(("project", project))
    return out


def load(project_dir=None, texts=None):
    """Read and merge every policy file that applies to ``project_dir``.

    Never raises. A rejected file is left out whole and reported in
    ``errors``; ``fail_closed`` is true when any file that loaded, or any
    rejected file's text, asks for it.

    ``texts`` maps a source (``"system"``) to bytes already read from it: that layer is parsed from those bytes
    instead of being opened again, so a caller that authenticated the bytes judges exactly what it authenticated.
    """
    layers, errors, paths = [], [], []
    fail_closed = False
    texts = texts or {}
    for source, path in layer_paths(project_dir):
        given = texts.get(source)
        try:
            if given is not None:
                layer = parse_layer(given.decode("utf-8") if isinstance(given, bytes) else given, source, path)
            else:
                layer = load_layer(path, source)
        except (PolicyError, UnicodeDecodeError) as exc:
            if isinstance(exc, UnicodeDecodeError):
                exc = PolicyError(f"cannot read: {exc}", path)
            errors.append((source, path, str(exc)))
            paths.append((source, path, False))
            fail_closed = fail_closed or declares_fail_closed(
                path, given.decode("utf-8", "replace") if isinstance(given, bytes) else given)
            continue
        if layer is None:
            continue
        paths.append((source, path, True))
        layers.append(layer)
    policy = None
    # The strongest file that exists is the base. When it is the rejected one
    # there is no policy at all: promoting a weaker file to base would let a
    # project policy allow what the broken user policy denied.
    base_ok = bool(paths) and paths[0][2]
    if layers and base_ok:
        try:
            policy = merge(layers)
        except PolicyError as exc:  # a preset that cannot be built: a packaging error
            errors.append(("preset", "", str(exc)))
        else:
            fail_closed = fail_closed or bool(policy.settings["fail_closed"])
    return Loaded(policy, errors, paths, fail_closed)
