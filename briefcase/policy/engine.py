"""Evaluate one tool call against a policy.

A call is broken into *units*, and each unit is judged on its own:

- a shell command line: every simple command it would run, every file path
  those commands name, and every reason the line could not be analysed
- a file read or write: the path
- an MCP call: the server and tool name
- a web request: the host

For one unit, the built-in protection rules are checked first. Then, in each
rule group, the first rule that matches decides (the base policy's rules, then
the preset's; a restrict-only policy is a group of its own). The unit's result
is the most restrictive of those, and the call's result is the most
restrictive of its units: ``deny`` beats ``ask`` beats ``allow``.

The effect vocabulary is the guardrail framework's (``allow``/``deny``) plus
``ask``; :mod:`briefcase.policy.guardrail` exposes this engine as a
``GuardrailEnv``. This module avoids ``dataclasses`` and ``enum`` because it
runs in the hook before every tool call.
"""

from __future__ import annotations

import fnmatch
import os
import re
import sys

from briefcase.policy.model import RANK

CASE_INSENSITIVE = sys.platform == "darwin" or sys.platform.startswith("win")


class ToolCall(object):
    """A tool call in tool-neutral form. ``kind`` is one of ``shell``, ``read``,
    ``write``, ``mcp``, ``web`` or ``other`` (never judged)."""

    __slots__ = ("kind", "tool", "tool_name", "command", "cwd", "paths", "server", "name", "url",
                 "project_dir", "dialect", "session_id", "tool_use_id", "note", "defect")

    def location(self):
        """The directory this call is judged from: where project policy is found and relative paths are read. One
        definition for the validator, the policy loader and the evaluator."""
        return self.project_dir or self.cwd

    def __init__(self, kind, tool="", tool_name="", command=None, cwd=None, paths=(), server=None, name=None,
                 url=None, project_dir=None, dialect="posix", session_id=None, tool_use_id=None, note=None,
                 defect=None):
        self.kind = kind
        self.tool = tool
        self.tool_name = tool_name
        self.command = command
        self.cwd = cwd
        self.paths = list(paths)
        self.server = server
        self.name = name
        self.url = url
        self.project_dir = project_dir
        self.dialect = dialect
        self.session_id = session_id
        self.tool_use_id = tool_use_id
        self.note = note
        # What is wrong with an otherwise judgeable event (no event name, an empty command, an unrecognised tool).
        # A lenient hook still judges the call; a fail-closed one denies it.
        self.defect = defect

    def target(self):
        """The text a decision is about: the command, the path, ``server.tool`` or the URL."""
        if self.kind == "shell":
            return self.command or ""
        if self.kind in ("read", "write"):
            return ", ".join(self.paths)
        if self.kind == "mcp":
            return f"{self.server or '?'}.{self.name or '?'}"
        if self.kind == "web":
            return self.url or ""
        return self.tool_name


class Match(object):
    """Why one unit got its effect."""

    __slots__ = ("effect", "rule_id", "source", "reason", "unit", "detail")

    def __init__(self, effect, rule_id, source, reason, unit, detail=""):
        self.effect = effect
        self.rule_id = rule_id
        self.source = source
        self.reason = reason
        self.unit = unit
        self.detail = detail

    def to_dict(self):
        out = {"effect": self.effect, "rule": self.rule_id, "source": self.source, "unit": self.unit}
        if self.reason:
            out["reason"] = self.reason
        if self.detail:
            out["detail"] = self.detail
        return out


class Decision(object):
    """``effect`` is what the tool is told. ``would`` is what the rules said;
    the two differ in observe mode, where everything is allowed and logged."""

    __slots__ = ("effect", "would", "match", "matches", "units", "problems", "mode", "kind", "explicit_allow")

    def __init__(self, effect, would, match, matches, units, problems, mode, kind, explicit_allow=False):
        self.explicit_allow = explicit_allow  # did the policy itself say allow (a rule, or a `default` it declares)?
        self.effect = effect
        self.would = would
        self.match = match
        self.matches = matches
        self.units = units
        self.problems = problems
        self.mode = mode
        self.kind = kind

    @property
    def rule_id(self):
        return self.match.rule_id if self.match else None

    @property
    def reason(self):
        return self.match.reason if self.match else ""

    def to_dict(self):
        out = {"effect": self.effect, "would": self.would, "mode": self.mode, "kind": self.kind,
               "rule": self.rule_id, "source": self.match.source if self.match else None,
               "reason": self.reason, "matches": [m.to_dict() for m in self.matches],
               "units": list(self.units), "problems": list(self.problems)}
        return out


class Context(object):
    __slots__ = ("settings", "home", "project", "tmp")

    def __init__(self, settings, home, project):
        self.settings = settings
        self.home = home
        self.project = project
        self.tmp = os.environ.get("TMPDIR") or "/tmp"


# --- path patterns ------------------------------------------------------------

def glob_to_regex(pattern):
    """A glob where ``*`` and ``?`` stop at ``/`` and ``**`` does not."""
    out = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
            elif pattern.startswith("**", i):
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = pattern.find("]", i + 2)
            if j < 0:
                out.append(re.escape(c))
                i += 1
            else:
                body = pattern[i + 1:j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def expand_pattern(pattern, ctx):
    """Absolute glob(s) for one ``paths`` entry.

    ``~`` is the home directory, ``${project}`` the project directory and
    ``${tmp}`` the temp directory. A pattern that is not anchored that way
    matches at any depth, like a ``.gitignore`` line. When the fixed leading
    directory of a pattern is a symlink, the resolved form is matched too.
    """
    text = pattern.replace("\\", "/") if os.sep == "\\" else pattern
    if text.startswith("${project}"):
        if not ctx.project:
            return []
        text = ctx.project + text[len("${project}"):]
    elif text.startswith("${tmp}"):
        text = ctx.tmp + text[len("${tmp}"):]
    elif text.startswith("${home}"):
        text = ctx.home + text[len("${home}"):]
    elif text == "~" or text.startswith("~/"):
        text = ctx.home + text[1:]
    if not os.path.isabs(text):
        return ["**/" + (text[2:] if text.startswith("./") else text)]
    text = text.rstrip("/") or "/"
    out = [text]
    cut = len(text)
    for index, ch in enumerate(text):
        if ch in "*?[":
            cut = text.rfind("/", 0, index)
            break
    prefix = text[:cut] or "/"
    try:
        real = os.path.realpath(prefix)
    except OSError:
        real = prefix
    if real != prefix:
        out.append(real + text[cut:])
    return out


def compile_paths(patterns, ctx):
    """One regex matching any of ``patterns``, or anything beneath a match."""
    parts = []
    for pattern in patterns:
        for expanded in expand_pattern(pattern, ctx):
            parts.append(glob_to_regex(expanded) + "(?:/.*)?")
    if not parts:
        return None
    return re.compile("(?:" + "|".join(parts) + r")\Z", re.IGNORECASE if CASE_INSENSITIVE else 0)


def path_forms(path, cwd=None, home=None):
    """The spellings of ``path`` a rule is matched against: with ``..`` folded,
    and with symlinks resolved. A rule on ``~/.ssh`` must hold for a symlink
    that points into it, and for a path that climbs out and back in."""
    text = path
    if text == "~" or text.startswith("~/"):
        text = (home or os.path.expanduser("~")) + text[1:]
    if not os.path.isabs(text):
        text = os.path.join(cwd or os.getcwd(), text)
    lexical = os.path.normpath(text)
    if lexical.startswith("//"):          # POSIX keeps exactly two leading slashes; it is still "/"
        lexical = "/" + lexical.lstrip("/")
    forms = [lexical]
    try:
        real = os.path.realpath(lexical)
    except (OSError, ValueError):
        real = lexical
    if real != lexical:
        forms.append(real)
    return forms


def _under(path, directory):
    directory = directory.rstrip("/") or "/"
    if CASE_INSENSITIVE:
        path, directory = path.lower(), directory.lower()
    return path == directory or path.startswith(directory.rstrip("/") + "/")


class _Compiled(object):
    __slots__ = ("commands", "paths", "excepts", "outside")


def _compiled(rule, ctx):
    compiled = rule._compiled
    if compiled is None:
        compiled = _Compiled()
        compiled.commands = None
        compiled.paths = compile_paths(rule.paths, ctx) if rule.paths else None
        compiled.excepts = compile_paths(rule.excepts, ctx) if rule.excepts else None
        outside = []
        for directory in rule.outside:
            for expanded in expand_pattern(directory, ctx):
                if os.path.isabs(expanded):
                    outside.append(expanded)
        compiled.outside = outside
        rule._compiled = compiled
    return compiled


# --- rule matching ------------------------------------------------------------

def _match_command(rule, cmd, ctx):
    if "shell" not in rule.kinds:
        return None
    if rule.check:
        from briefcase.policy import presets, shell
        check = presets.CHECKS[rule.check]
        for candidate in shell.suffixes(cmd):     # also behind a wrapper this code does not know
            detail = check(candidate, ctx)
            if detail:
                return detail
        return None
    if not rule.command:
        return "any command"
    from briefcase.policy import shell
    compiled = _compiled(rule, ctx)
    if compiled.commands is None:
        compiled.commands = [shell.compile_pattern(text) for text in rule.command]
    anywhere = rule.effect != "allow"
    for pattern in compiled.commands:
        if shell.pattern_matches(pattern, cmd, anywhere):
            return f"matches `{pattern.text}`"
    return None


def _match_path(rule, forms, access, ctx):
    """``forms`` are the spellings of one path. A deny or ask rule matches when
    any spelling does; an allow rule only when all do, so a symlink cannot
    borrow the allowance of the directory it sits in."""
    kinds = rule.kinds
    if access == "any":
        if "read" not in kinds and "write" not in kinds:
            return None
    elif access not in kinds:
        return None
    compiled = _compiled(rule, ctx)
    if not rule.paths and not rule.outside:
        return "any path"

    def one(form):
        if compiled.excepts is not None and compiled.excepts.match(form):
            return False
        if rule.paths and not (compiled.paths is not None and compiled.paths.match(form)):
            return False
        if rule.outside and any(_under(form, directory) for directory in compiled.outside):
            return False
        return True

    hits = [form for form in forms if one(form)]
    if not hits:
        return None
    if rule.effect == "allow" and len(hits) != len(forms):
        return None
    if rule.outside and not rule.paths:
        return "outside " + ", ".join(rule.outside)
    if hits[0] != forms[0]:
        return f"resolves to {hits[0]}"
    return "matches the rule's paths"


def _globs(value, patterns):
    return any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns)


def _match_mcp(rule, server, name):
    if "mcp" not in rule.kinds:
        return None
    if rule.server and not (server is not None and _globs(server, rule.server)):
        return None
    if rule.name and not (name is not None and _globs(name, rule.name)):
        return None
    return "any MCP tool" if not (rule.server or rule.name) else "matches the rule's server/name"


def host_of(url):
    """Lower-cased host of a URL, without credentials, port or trailing dot."""
    if not url or not isinstance(url, str):
        return None
    rest = url.strip()
    scheme = rest.find("://")
    if scheme >= 0:
        rest = rest[scheme + 3:]
    for stop in "/?#\\":
        cut = rest.find(stop)
        if cut >= 0:
            rest = rest[:cut]
    rest = rest.rsplit("@", 1)[-1]
    if rest.startswith("["):
        end = rest.find("]")
        host = rest[1:end] if end > 0 else rest[1:]
    else:
        host = rest.split(":", 1)[0]
    host = host.strip().rstrip(".").lower()
    return host or None


def _match_web(rule, host):
    if "web" not in rule.kinds:
        return None
    if not rule.hosts:
        return "any web request"
    if host is None:
        return None
    for pattern in rule.hosts:
        pattern = pattern.lower()
        if fnmatch.fnmatchcase(host, pattern) or (pattern.startswith("*.") and host == pattern[2:]):
            return f"host matches {pattern}"
    return None


# --- evaluation ---------------------------------------------------------------

def _first(rules, test):
    for rule in rules:
        detail = test(rule)
        if detail:
            return rule, detail
    return None, None


def _judge(policy, unit, test, primary):
    """Matches for one unit: protection first, then the first hit in each group."""
    out = []
    rule, detail = _first(policy.protect, test)
    if rule is not None:
        out.append(Match(rule.effect, rule.id, rule.source, rule.reason, unit, detail))
    for index, group in enumerate(policy.groups):
        rule, detail = _first(group, test)
        if rule is not None:
            out.append(Match(rule.effect, rule.id, rule.source, rule.reason, unit, detail))
        elif index == 0 and primary and policy.settings["default"] != "allow":
            out.append(Match(policy.settings["default"], "default", "setting",
                             f"No rule matched and the policy's default is {policy.settings['default']}.", unit))
    return out


def needles(policy, ctx):
    """Path fragments worth searching for in text that cannot be analysed
    (inline interpreter code, PowerShell). Taken from the deny and ask path rules."""
    out = []
    for rule in policy.protect + policy.all_rules():
        if rule.effect == "allow" or not rule.paths:
            continue
        for pattern in rule.paths:
            text = pattern
            for prefix in ("~/", "${project}/", "${home}/", "**/", "./"):
                if text.startswith(prefix):
                    text = text[len(prefix):]
            for index, ch in enumerate(text):
                if ch in "*?[":
                    text = text[:index]
                    break
            text = text.strip("/")
            if len(text) >= 4 and (rule, text) not in out:
                out.append((rule, text))
    return out


def _mentions(text, needle):
    """Is ``needle`` in ``text`` as a path component, not as part of a longer word?
    (``.env`` must not match ``os.environ``.)"""
    haystack = text.lower() if CASE_INSENSITIVE else text
    needle = needle.lower() if CASE_INSENSITIVE else needle
    start = 0
    while True:
        at = haystack.find(needle, start)
        if at < 0:
            return False
        before = haystack[at - 1] if at else " "
        after = haystack[at + len(needle)] if at + len(needle) < len(haystack) else " "
        if not (before.isalnum() or before in "_)]") and not (after.isalnum() or after == "_"):
            return True
        start = at + 1


def _shell_units(policy, call, ctx, matches, units, problems):
    from briefcase.policy import shell
    command = call.command or ""
    if call.dialect != "posix":
        problems.append(f"{call.dialect} commands are not analysed")
        units.append(f"{call.dialect}: {_clip(command)}")
        _scan_text(policy, ctx, command, f"{call.dialect} command", matches)
        return
    analysis = shell.analyze(command, cwd=call.cwd, home=ctx.home)
    problems.extend(analysis.problems)
    for cmd in analysis.commands:
        via = f" (via {' > '.join(cmd.via)})" if cmd.via else ""
        unit = "command: " + _clip(cmd.text()) + via
        units.append(unit)
        matches.extend(_judge(policy, unit, lambda rule, cmd=cmd: _match_command(rule, cmd, ctx), True))
        for path, access, written in shell.path_refs(cmd):
            forms = path_forms(path)
            verb = {"read": "read", "write": "written"}.get(access, "used")
            label = f"path: {_clip(written)} ({verb} by {cmd.name or 'a command'})"
            matches.extend(_judge(
                policy, label, lambda rule, forms=forms, access=access: _match_path(rule, forms, access, ctx),
                False))
        if cmd.opaque:
            _scan_text(policy, ctx, cmd.opaque, f"inline {cmd.name} code", matches)


def _scan_text(policy, ctx, text, what, matches):
    for rule, needle in needles(policy, ctx):
        if _mentions(text, needle):
            matches.append(Match(rule.effect, rule.id, rule.source, rule.reason, f"{what} mentions {needle}",
                                 "found by text search; this code is not analysed"))
            return


def _clip(text, limit=100):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def evaluate(policy, call):
    """Judge ``call`` under ``policy``. Pure: no I/O beyond resolving paths."""
    settings = policy.settings
    home = os.path.expanduser("~")
    ctx = Context(settings, home, call.location())
    matches, units, problems = [], [], []
    kind = call.kind
    if kind == "shell":
        _shell_units(policy, call, ctx, matches, units, problems)
        for problem in problems:
            effect = settings["on_unparseable"]
            matches.append(Match(effect, "on_unparseable", "setting",
                                 "This command could not be fully analysed: " + problem + ".",
                                 "unparseable: " + problem))
    elif kind in ("read", "write"):
        for path in call.paths:
            forms = path_forms(path, call.cwd, home)
            unit = f"{kind}: {path}" + (f" -> {forms[-1]}" if len(forms) > 1 else "")
            units.append(unit)
            matches.extend(_judge(policy, unit,
                                  lambda rule, forms=forms: _match_path(rule, forms, kind, ctx), True))
    elif kind == "mcp":
        unit = f"mcp: {call.server or '?'}.{call.name or '?'}"
        units.append(unit)
        matches.extend(_judge(policy, unit, lambda rule: _match_mcp(rule, call.server, call.name), True))
    elif kind == "web":
        host = host_of(call.url)
        unit = f"web: {host or 'no host'}"
        units.append(unit)
        matches.extend(_judge(policy, unit, lambda rule: _match_web(rule, host), True))
    top = None
    for match in matches:       # the first match with the most restrictive effect explains the decision
        if top is None or RANK[match.effect] > RANK[top.effect]:
            top = match
    would = top.effect if top is not None else "allow"
    mode = settings["mode"]
    effect = would if mode == "enforce" else "allow"
    allowed_units = {m.unit for m in matches if m.effect == "allow"}
    explicit = would == "allow" and bool(units) and (
        (policy.default_declared and settings["default"] == "allow") or all(u in allowed_units for u in units))
    return Decision(effect, would, top, matches, units, problems, mode, kind, explicit)
