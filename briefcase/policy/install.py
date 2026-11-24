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
    require_policy_sha: str = ""  # a managed hook names the system policy it requires, by hash
    accept_codex_startup_risk: bool = False  # Codex cannot be made to block when its hook executable is missing


CODEX_STARTUP_RISK = (
    "Codex starts the hook command itself. If its executable is missing or not on PATH when Codex starts, the "
    "command fails with a status Codex treats as an ordinary hook failure and runs the tool anyway; "
    "briefcase-ai cannot block that, because none of its code has started. Once the hook runs, --fail-closed "
    "denies an unreadable policy, malformed input, a missing required policy and a crash, and exits 2 with a reason "
    "on stderr when it cannot write its verdict. It does not cover a missing executable, a host timeout, or "
    "stdout and stderr both being closed")


def launcher_path() -> str:
    return str(Path(__file__).with_name("hook_main.py"))


_STRICT_START = ("import sys\n"
                 "try:\n"
                 "    import runpy\n"
                 "    status = runpy.run_path({path})['main']()\n"
                 "    assert isinstance(status, int)\n"
                 "except BaseException as stop:\n"
                 "    if isinstance(stop, SystemExit) and isinstance(stop.code, int):\n"
                 "        sys.exit(stop.code)\n"
                 "    sys.stderr.write('Blocked by briefcase-ai policy: the hook launcher could not run and the hook "
                 "is fail-closed. Reinstall with `briefcase-ai policy init`.\\n')\n"
                 "    sys.exit(2)\n"
                 "sys.exit(status)\n")


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
    - fail closed: the launcher is loaded by a short ``-c`` program that calls its
      named ``main()`` and exits 2 with a reason when the file is missing, cannot
      be loaded or has no working ``main``, so that blocks, even in Codex, which
      has no fail-closed setting.

    Checked against Claude Code 2.1.330 (``briefcase-ai policy`` E2E run, 2026-11-22)."""
    flags_py = ["-I"] + (["-S"] if sys.version_info >= (3, 11) else [])
    if options.portable:
        base = ["briefcase-ai", "hook", tool]
    elif options.fail_closed:
        # The launcher is loaded and its named main() called here, so a file that is missing, truncated or without a
        # working main is a block (exit 2 with a reason), not a silent exit 0.
        code = "exec(" + repr(_STRICT_START.format(path=json.dumps(launcher_path()))) + ")"
        base = [sys.executable] + flags_py + ["-c", code, tool]
    else:
        code = f'import runpy; runpy.run_path({json.dumps(launcher_path())}, run_name="__main__")'
        base = [sys.executable] + flags_py + ["-c", code, tool]
    flags = ["--enforce"]
    if options.fail_closed:
        flags.append("--fail-closed")
    if options.capture_content:
        flags.append("--capture-content")
    flags += ["--endpoint", (options.endpoint or DEFAULT_ENDPOINT).rstrip("/")]  # always explicit
    if options.require_policy_sha:
        flags += ["--require-policy-sha256", options.require_policy_sha]
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
        if self.options.fail_closed:
            # A startup guarantee that cannot be met is refused, not installed with a note.
            from briefcase.connect.adapters import ProfileError

            if self.tool == "codex" and not self.options.accept_codex_startup_risk:
                raise ProfileError(CODEX_STARTUP_RISK + ". Pass --accept-codex-startup-risk to install it anyway, "
                                   "knowing that")

            if self.options.portable and not shutil.which("briefcase-ai"):
                raise ProfileError("--fail-closed needs `briefcase-ai` on PATH for the portable hook, and it is "
                                   "not: a hook that cannot start would not block")
            if not self.options.portable and not (os.path.exists(sys.executable)
                                                  and os.path.exists(launcher_path())):
                raise ProfileError("--fail-closed needs this Python and the hook launcher to exist, and one is "
                                   "missing: a hook that cannot start would not block")
        if self.options.portable and not shutil.which("briefcase-ai"):
            notes.append("`briefcase-ai` is not on PATH here; the hook will fail open until it is")
        if self.tool == "claude-code":
            path = ctx.project_dir / ".claude" / "settings.json" if project else self.path("user_settings")
            field = self.enforce["fail_closed_field"]
            supports = True
            if self.options.fail_closed:
                version = parse_version(self.version())
                if version is None:
                    from briefcase.connect.adapters import ProfileError

                    raise ProfileError(f"--fail-closed needs Claude Code {field['min_version']}+ for "
                                       f"`{field['name']}: \"{field['value']}\"`, and the installed version could "
                                       "not be read: without it a hook that cannot start would not block")
                elif version < (parse_version(field["min_version"]) or (0,)):
                    from briefcase.connect.adapters import ProfileError

                    raise ProfileError(f"--fail-closed needs Claude Code {field['min_version']}+ (it has "
                                       f"{'.'.join(map(str, version))}): older versions ignore `{field['name']}`, "
                                       "so a hook that cannot start would not block")
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
            if self.options.fail_closed:
                notes.append("fail-closed: " + CODEX_STARTUP_RISK)
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


def hook_count(tool: str) -> int:
    """How many enforcement hook entries for ``tool`` are on disk (see :func:`hook_active`)."""
    from briefcase.connect.adapters import Context

    needle, total = marker(tool), 0
    seen = set()
    for scope in ("user", "project"):
        try:
            items = adapters(HookOptions())[PREFIX + tool].plan(Context(scope=scope, mcp=False, telemetry=False))
        except Exception:
            continue
        for item in items:
            if isinstance(item, FileEdit) and str(item.path) not in seen:
                seen.add(str(item.path))
                try:
                    total += edits.read_text(item.path).count(needle)
                except (OSError, UnicodeDecodeError):
                    continue
    return total


def hook_active(tool: str) -> bool:
    """Is any enforcement hook for ``tool`` on disk, wherever it came from? Read from the files the tool reads, not
    from the journal: a copy a user edited, or one the journal lost, still enforces (and still blocks)."""
    from briefcase.connect.adapters import Context

    needle = marker(tool)
    for scope in ("user", "project"):
        adapters_ = adapters(HookOptions())
        try:
            items = adapters_[PREFIX + tool].plan(Context(scope=scope, mcp=False, telemetry=False))
        except Exception:
            continue
        for item in items:
            if isinstance(item, FileEdit):
                try:
                    if needle in edits.read_text(item.path):
                        return True
                except (OSError, UnicodeDecodeError):
                    continue
    return False


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
