"""Install and remove the enforcement hooks through the ``briefcase-ai connect`` machinery.

Every write is a :class:`~briefcase.connect.edits.FileEdit`: key-level, with a
diff, one confirmation, a timestamped backup and a journal entry, exactly like
``briefcase-ai connect``. The entries are journalled under ``policy:<tool>`` so
``briefcase-ai policy off`` removes the hooks and nothing else, while
``briefcase-ai connect --undo`` (no tool named) still removes everything.

The entry written to each tool's file is described under ``enforce`` in
``briefcase/connect/adapters.json`` with its documentation URL.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from briefcase.connect import edits
from briefcase.connect.adapters import DEFAULT_ENDPOINT, Adapter, Context, load_data, parse_version
from briefcase.connect.edits import FileEdit, Manual
from briefcase.policy import tools as tool_formats

PREFIX = "policy:"
TOOLS = tool_formats.TOOLS


@dataclass
class HookOptions:
    fail_closed: bool = False
    capture_content: bool = False
    endpoint: str = DEFAULT_ENDPOINT
    portable: bool = False


def launcher_path() -> str:
    return str(Path(__file__).with_name("hook_main.py"))


def hook_command(tool: str, options: HookOptions) -> str:
    """The command a tool runs before each tool call.

    The default form names this Python and the launcher file by absolute path:
    it starts in about a quarter of the time of ``briefcase-ai hook``, which has
    to import the SDK first. ``portable`` gives the form that works on any
    machine with ``briefcase-ai`` on PATH, for files that are shared.

    How the command fails when the launcher file is gone (briefcase upgraded
    into another environment, a virtualenv deleted) matters, because Python
    exits with code 2 when the script it is given does not exist, and exit
    code 2 *blocks* the tool call in Claude Code, Cursor and Codex. So:

    - fail open (default): the launcher is started through ``-c`` and
      ``runpy``; a missing file is an ordinary exception, exit code 1, which
      every tool treats as a failed hook and lets the call through.
    - fail closed: the launcher is passed as the script, so a missing file is
      exit code 2 and blocks, even in Codex, which has no fail-closed setting.

    Checked against Claude Code 2.1.330 (``briefcase-ai policy`` E2E run, 2026-11-22)."""
    flags_py = ["-I"] + (["-S"] if sys.version_info >= (3, 11) else [])
    if options.portable:
        base = ["briefcase-ai", "hook", tool]
    elif options.fail_closed:
        base = [sys.executable] + flags_py + [launcher_path(), tool]
    else:
        code = f'import runpy; runpy.run_path({json.dumps(launcher_path())}, run_name="__main__")'
        base = [sys.executable] + flags_py + ["-c", code, tool]
    flags = ["--enforce"]
    if options.fail_closed:
        flags.append("--fail-closed")
    if options.capture_content:
        flags.append("--capture-content")
    if options.endpoint and options.endpoint.rstrip("/") != DEFAULT_ENDPOINT:
        flags += ["--endpoint", options.endpoint.rstrip("/")]
    return " ".join(shlex.quote(part) for part in base + flags)


def marker(tool: str) -> str:
    """A substring only our entry for ``tool`` contains, in either command form."""
    return f" {tool} --enforce"


def claude_entry(command: str, enforce: Dict[str, Any], fail_closed: bool, with_failure_field: bool = True
                 ) -> Dict[str, Any]:
    handler: Dict[str, Any] = {"type": "command", "command": command, "timeout": enforce["timeout_seconds"]}
    if fail_closed and with_failure_field:
        field = enforce["fail_closed_field"]
        handler[field["name"]] = field["value"]
    return {"matcher": tool_formats.CLAUDE_MATCHER, "hooks": [handler]}


def codex_entry(command: str, enforce: Dict[str, Any]) -> Dict[str, Any]:
    return {"matcher": tool_formats.CODEX_MATCHER,
            "hooks": [{"type": "command", "command": command, "timeout": enforce["timeout_seconds"]}]}


def cursor_entries(command: str, enforce: Dict[str, Any], fail_closed: bool) -> Dict[str, Dict[str, Any]]:
    out = {}
    for event in tool_formats.CURSOR_EVENTS:
        entry: Dict[str, Any] = {"command": command, "timeout": enforce["timeout_seconds"]}
        if event == "preToolUse":
            entry["matcher"] = enforce["pretooluse_matcher"]
        if fail_closed:
            field = enforce["fail_closed_field"]
            entry[field["name"]] = field["value"]
        out[event] = entry
    return out


class PolicyAdapter(Adapter):
    """One tool's enforcement hooks, in the shape ``briefcase.connect.engine`` plans and applies."""

    def __init__(self, tool: str, data: Dict[str, Any], options: HookOptions) -> None:
        Adapter.__init__(self, data)
        self.tool = tool
        self.id = PREFIX + tool
        self.enforce: Dict[str, Any] = data["enforce"]
        self.name = f"{data['name']} policy hooks"
        self.mode = self.enforce["mode"]
        self.docs_url = self.enforce["docs_url"]
        self.verified_on = self.enforce["verified_on"]
        self.needs_protobuf = False
        self.options = options

    def _plan(self, ctx: Context) -> List[Any]:
        command = hook_command(self.tool, self.options)
        project = ctx.scope == "project"
        notes: List[str] = []
        if self.options.portable and not shutil.which("briefcase-ai"):
            notes.append("`briefcase-ai` is not on PATH here; the hook will fail open until it is")
        if self.tool == "claude-code":
            path = ctx.project_dir / ".claude" / "settings.json" if project else self.path("user_settings")
            field = self.enforce["fail_closed_field"]
            supports = True
            if self.options.fail_closed:
                version = parse_version(self.version())
                if version is None:
                    notes.append(f"fail_closed: `{field['name']}: \"{field['value']}\"` needs Claude Code "
                                 f"{field['min_version']}+; the installed version could not be read, so it is "
                                 "written anyway")
                elif version < (parse_version(field["min_version"]) or (0,)):
                    supports = False
                    notes.append(f"fail_closed: Claude Code {'.'.join(map(str, version))} is older than "
                                 f"{field['min_version']}, so `{field['name']}` is left out; a hook that cannot "
                                 "start will not block there")
            entry = claude_entry(command, self.enforce, self.options.fail_closed, supports)
            notes.append("a denied or asked call shows the rule's reason in Claude Code; allowed calls go through "
                         "Claude Code's normal permission prompts")
            return [FileEdit(self.id, path, "json", "Run the briefcase-ai policy before each tool call (PreToolUse)",
                             ops=[{"op": "append", "path": ["hooks", "PreToolUse"], "value": entry,
                                   "owned_by": marker(self.tool)}], notes=notes)]
        if self.tool == "cursor":
            path = ctx.project_dir / self.data["files"]["project_hooks"] if project else self.path("user_hooks")
            ops: List[Dict[str, Any]] = [{"op": "default", "path": ["version"], "value": 1}]
            for event, entry in cursor_entries(command, self.enforce, self.options.fail_closed).items():
                ops.append({"op": "append", "path": ["hooks", event], "value": entry,
                            "owned_by": marker(self.tool)})
            notes += ["experimental: taken from Cursor's documentation, never run against Cursor",
                      "Cursor can ask only for shell commands and MCP tools; for file reads and writes an "
                      "`ask` rule becomes the policy's ask_fallback"]
            return [FileEdit(self.id, path, "json",
                             "Run the briefcase-ai policy before shell commands, MCP tools, file reads and writes",
                             ops=ops, notes=notes)]
        if self.tool == "codex":
            if project:
                path = ctx.project_dir / ".codex" / "hooks.json"
            else:
                path = self.path("user_hooks")
            notes += ["experimental: taken from Codex's documentation, never run against Codex",
                      "Codex skips a new hook until you trust it: open /hooks in Codex once after this",
                      "Codex cannot ask from a hook, so an `ask` rule becomes the policy's ask_fallback"]
            return [FileEdit(self.id, path, "json", "Run the briefcase-ai policy before each tool call (PreToolUse)",
                             ops=[{"op": "append", "path": ["hooks", "PreToolUse"],
                                   "value": codex_entry(command, self.enforce), "owned_by": marker(self.tool)}],
                             notes=notes)]
        return [Manual(self.id, "No enforcement hook", "", f"{self.name} has no documented blocking hook")]


def adapters(options: Optional[HookOptions] = None) -> Dict[str, PolicyAdapter]:
    data = load_data()["tools"]
    options = options or HookOptions()
    return {PREFIX + tool: PolicyAdapter(tool, data[tool], options) for tool in TOOLS}


def detected(all_adapters: Dict[str, PolicyAdapter]) -> List[str]:
    return [name for name, adapter in all_adapters.items() if adapter.detect().installed]


def installed() -> List[Dict[str, Any]]:
    """What the journal says is installed, checked against the files as they are now.

    One row per tool: ``{"tool", "files": [(ok, what)], "command", "fail_closed", "launcher_ok"}``."""
    try:
        journal = edits.load_journal()
    except edits.LegacyJournal:
        return []
    rows = []
    for tool in TOOLS:
        owner = PREFIX + tool
        checks: List[Any] = []
        command = None
        for path, entry in journal["files"].items():
            mine = [op for op in entry["ops"] if op.get("tool") == owner]
            if not mine:
                continue
            checks.extend(edits.check_entry(path, entry, owner))
            for op in mine:
                found = _command_in(op.get("value"))
                if found:
                    command = found
        if not checks:
            continue
        launcher_ok = True
        if command:
            try:
                parts = shlex.split(command)
            except ValueError:
                parts = []
            if parts and os.path.isabs(parts[0]):
                launcher_ok = os.path.exists(parts[0]) and all(
                    os.path.exists(path) for path in _launcher_paths(parts))
            elif parts:
                launcher_ok = shutil.which(parts[0]) is not None
        rows.append({"tool": tool, "files": checks, "command": command,
                     "fail_closed": bool(command and "--fail-closed" in command), "launcher_ok": launcher_ok})
    return rows


def _launcher_paths(parts: List[str]) -> List[str]:
    """The launcher file(s) a hook command names: an absolute ``*.py`` argument, or the
    path inside the ``-c "import runpy; runpy.run_path('...')"`` form."""
    import re

    found = []
    for part in parts[1:]:
        if os.path.isabs(part) and part.endswith(".py"):
            found.append(part)
        for match in re.finditer(r"run_path\((['\"])(.+?)\1", part):
            found.append(match.group(2))
    return found


def _command_in(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        if isinstance(value.get("command"), str) and "--enforce" in value["command"]:
            return value["command"]
        for item in value.get("hooks", []) if isinstance(value.get("hooks"), list) else []:
            found = _command_in(item)
            if found:
                return found
    return None
