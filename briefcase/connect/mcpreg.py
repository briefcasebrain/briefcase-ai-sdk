"""Read a tool's MCP registry without changing it, and decide whether an entry is exactly the one we registered.

Every lookup answers ``absent``, ``present`` or ``unknown``. A lookup that fails, times out or prints something
unexpected is ``unknown`` and never counts as "ours". Entries are compared as structure (command, argument list,
environment), never as substrings. The entry's contents are used for the comparison only and are never printed.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from briefcase.connect import adapters as _adapters
from briefcase.connect import edits

Runner = Callable[[List[str]], Tuple[int, str]]

ABSENT, PRESENT, UNKNOWN = "absent", "present", "unknown"
OURS, DIFFERS = "ours", "differs"

# CLIs whose ``add`` replaces a same-named entry silently: never added without looking first.
REPLACING = ("codex", "code")


def _tail(argv: List[str]) -> List[str]:
    return argv[argv.index("--") + 1:] if "--" in argv else argv[argv.index("add") + 2:] if "add" in argv else []


def server_name(argv: List[str]) -> str:
    if argv[:1] == ["claude"]:
        return argv[5] if len(argv) > 5 else ""
    if argv[:1] == ["codex"]:
        return argv[3] if len(argv) > 3 else ""
    return "briefcase"


def expected_entry(argv: List[str]) -> Dict[str, Any]:
    """The registration ``argv`` creates, as structure."""
    if argv[:1] == ["code"]:  # code --add-mcp '{"name": ..., "command": ..., "args": [...]}'
        try:
            payload = json.loads(argv[2])
        except (IndexError, ValueError):
            return {}
        return {"command": payload.get("command"), "args": list(payload.get("args") or []), "env": {}}
    tail = _tail(argv)
    return {"command": tail[0] if tail else None, "args": tail[1:], "env": {}}


def _entry_matches(entry: Dict[str, Any], want: Dict[str, Any]) -> bool:
    if not isinstance(entry, dict) or not want:
        return False
    extra = set(entry) - {"command", "args", "env", "type"}
    if extra or entry.get("type") not in (None, "stdio"):
        return False
    return (edits.typed_eq(entry.get("command"), want.get("command"))
            and edits.typed_eq(list(entry.get("args") or []), want.get("args", []))
            and not entry.get("env"))


def _read_json(path: Path) -> Tuple[str, Optional[Dict[str, Any]]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ABSENT, None
    except (OSError, UnicodeDecodeError):
        return UNKNOWN, None
    try:
        doc = json.loads(text, object_pairs_hook=edits._no_duplicates) if text.strip() else {}
    except ValueError:
        return UNKNOWN, None
    return (PRESENT, doc) if isinstance(doc, dict) else (UNKNOWN, None)


def _codex_file() -> Path:
    return _adapters.profile_path("codex", "user_config") or (edits.home() / ".codex" / "config.toml")


def _vscode_file() -> Optional[Path]:
    """The user ``mcp.json`` of the VS Code that ``code`` starts, or ``None`` when that cannot be told (a portable
    install, or a data directory chosen some other way)."""
    if os.environ.get("VSCODE_PORTABLE"):
        return None
    settings = _adapters.registry()["vscode-copilot"].path("user_settings")  # honours VSCODE_APPDATA and XDG
    return settings.with_name("mcp.json")


def _claude_config_entry(name: str, kind: str = "user") -> Any:
    """The server ``name`` as Claude Code's own files hold it, in the scope ``kind`` (``user``: ``~/.claude.json``
    ``mcpServers``; ``local``: that file's ``projects[<cwd>].mcpServers``; ``project``: ``./.mcp.json``). A dict, or
    ``None`` when there is no such file, ``"missing"`` when the file has no such entry, ``"unreadable"`` when it
    cannot be parsed."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    path = (Path(base).expanduser() if base else edits.home()) / ".claude.json"
    if kind == "project":
        path = Path(os.getcwd()) / ".mcp.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return "unreadable"
    try:
        doc = json.loads(text, object_pairs_hook=edits._no_duplicates)
    except ValueError:
        return "unreadable"
    holder: Any = doc
    if kind == "local":
        projects = doc.get("projects") if isinstance(doc, dict) else None
        holder = projects.get(os.getcwd()) if isinstance(projects, dict) else None
    servers = holder.get("mcpServers") if isinstance(holder, dict) else None
    if not isinstance(servers, dict) or name not in servers or not isinstance(servers[name], dict):
        return "missing"
    return servers[name]


def lookup(argv: List[str], runner: Runner) -> Tuple[str, Optional[Dict[str, Any]]]:
    """``(state, entry)`` for the registration ``argv`` would create. ``entry`` is the parsed entry when present."""
    name = server_name(argv)
    if argv[:1] == ["codex"]:
        path = _codex_file()
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ABSENT, None
        except (OSError, UnicodeDecodeError):
            return UNKNOWN, None
        if edits._toml is None:
            return UNKNOWN, None
        try:
            doc = edits._toml.loads(text)
        except Exception:
            return UNKNOWN, None
        servers = doc.get("mcp_servers")
        if not isinstance(servers, dict) or name not in servers:
            return ABSENT, None
        entry = servers[name]
        return (PRESENT, entry) if isinstance(entry, dict) else (UNKNOWN, None)
    if argv[:1] == ["code"]:
        where = _vscode_file()
        if where is None:
            return UNKNOWN, None
        state, doc = _read_json(where)
        if state != PRESENT or doc is None:
            return state, None
        servers = doc.get("servers")
        if not isinstance(servers, dict) or name not in servers:
            return ABSENT, None
        entry = servers[name]
        return (PRESENT, entry) if isinstance(entry, dict) else (UNKNOWN, None)
    if argv[:1] == ["claude"]:
        code, output = runner(["claude", "mcp", "get", name])
        if code != 0:
            return (ABSENT, None) if re.search(r"no mcp server (found|named)", output, re.I) else (UNKNOWN, None)
        fields: Dict[str, str] = {}
        env_lines = 0
        in_env = False
        for line in output.splitlines():
            match = re.match(r"^\s*(Scope|Status|Type|Command|Args|URL|Environment|Headers):\s*(.*)$", line)
            if match:
                in_env = match.group(1) == "Environment"
                if in_env and match.group(2).strip():
                    env_lines += 1
                fields[match.group(1)] = match.group(2).strip()
            elif in_env and not line.strip():
                in_env = False  # a blank line ends the section; the CLI's removal hint follows it
            elif in_env and line.startswith("    "):
                env_lines += 1
        if "Command" not in fields or fields.get("Type", "stdio") != "stdio" or "URL" in fields:
            return UNKNOWN, None
        # The CLI prints the argument list on one line. It is read back as a list; if the line does not survive a
        # round trip (quotes, or spaces that split differently) the arguments are not known and neither is ownership.
        args_text = fields.get("Args", "").strip()
        try:
            args = shlex.split(args_text)
        except ValueError:
            return UNKNOWN, None
        if " ".join(args) != args_text:
            return UNKNOWN, None
        scope = fields.get("Scope", "").lower()
        kind = "user" if scope.startswith("user") else "project" if scope.startswith("project") else \
            "local" if scope.startswith("local") else None
        if kind is None:
            return UNKNOWN, None
        env = {"_": 1} if env_lines else {}
        found: Dict[str, Any] = {"command": fields["Command"], "args": args, "scope": kind, "env": env}
        structured = _claude_config_entry(name, kind)
        if structured == "unreadable":
            return UNKNOWN, None
        if isinstance(structured, dict):  # the tool's own file keeps argument boundaries the one-line listing loses
            found.update(args=list(structured.get("args") or []), command=structured.get("command"),
                         env=structured.get("env") or found["env"], structured=True)
        else:  # no file, or one that does not show this entry: the one-line listing cannot prove the boundaries
            found["boundaries_unproven"] = True
        return PRESENT, found
    return UNKNOWN, None


def owned(recorded_argv: List[str], runner: Runner) -> str:
    """Is the registry's entry exactly what ``recorded_argv`` registered? ``ours``, ``absent``, ``differs`` or
    ``unknown`` (the lookup could not say, which is never ``ours``)."""
    state, entry = lookup(recorded_argv, runner)
    if state != PRESENT or entry is None:
        return state
    want = expected_entry(recorded_argv)
    if recorded_argv[:1] == ["claude"]:
        recorded_scope = recorded_argv[4] if len(recorded_argv) > 4 else "user"
        if any(re.search(r"\s", a) for a in want.get("args", [])):
            return UNKNOWN  # the CLI's one-line listing cannot tell these arguments apart
        flat_same = " ".join(str(a) for a in entry.get("args") or []) == " ".join(want.get("args", []))
        if (entry.get("command") != want.get("command") or entry.get("scope") != recorded_scope or entry.get("env")
                or entry.get("type") not in (None, "stdio") or not flat_same):
            return DIFFERS  # differs however the arguments are split
        if entry.get("boundaries_unproven"):
            return UNKNOWN  # the file that would settle the argument boundaries does not show this entry
        same = entry.get("args") == want.get("args", [])
    else:
        same = _entry_matches(entry, want)
    return OURS if same else DIFFERS


def profile_var(argv: List[str]) -> str:
    """The environment variable that selects the profile (config location) of the CLI ``argv`` runs."""
    return {"codex": "CODEX_HOME", "code": "VSCODE_APPDATA"}.get(argv[0] if argv else "", "CLAUDE_CONFIG_DIR")


def profile_prefix(tool_argv: List[str], profile: Optional[str]) -> str:
    """The shell prefix that points a command at the profile it was recorded for."""
    return f"{profile_var(tool_argv)}={shlex.quote(profile)} " if profile else ""


def qualified(argv: List[str], profile: Optional[str], cwd: Optional[str] = None) -> str:
    """``argv`` as the exact command to paste, in the profile (and directory) it was recorded for."""
    text = profile_prefix(argv, profile) + " ".join(shlex.quote(a) for a in argv)
    return f"cd {shlex.quote(cwd)} && {text}" if cwd else text


def current_profile(argv: List[str]) -> Optional[str]:
    return os.environ.get(profile_var(argv))
