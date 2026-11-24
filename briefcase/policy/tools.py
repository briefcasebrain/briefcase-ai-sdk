"""Each coding tool's hook payload and response format.

Formats are taken from the tools' documentation; the URL and the date each was
checked are recorded under ``enforce`` in ``briefcase/connect/adapters.json``.

Claude Code (https://code.claude.com/docs/en/hooks, checked 2026-11-21)
    ``PreToolUse`` gets ``tool_name`` and ``tool_input`` on stdin. Exit 0 with
    ``hookSpecificOutput.permissionDecision`` of ``deny`` or ``ask`` and a
    ``permissionDecisionReason``. ``allow`` would skip Claude Code's own
    permission prompt, so an allowed call prints nothing and the normal
    permission flow continues. A hook that crashes, times out or prints
    invalid JSON does not block, unless the handler has ``onFailure: "block"``.

Cursor (https://cursor.com/docs/hooks, checked 2026-11-21)
    ``beforeShellExecution`` and ``beforeMCPExecution`` answer
    ``{"permission": "allow"|"deny"|"ask"}``; ``beforeReadFile`` and
    ``preToolUse`` only ``allow`` or ``deny``. A permission hook that exits 0
    with invalid JSON *blocks*, so every path here prints valid JSON. Crashes
    and timeouts fail open unless the hook entry has ``failClosed: true``.

Codex CLI (https://developers.openai.com/codex/hooks, checked 2026-11-21)
    ``PreToolUse`` can deny with the same ``hookSpecificOutput`` shape as
    Claude Code. ``permissionDecision: "ask"`` is not supported: Codex marks
    the hook as failed and runs the tool, so ``ask`` is never sent to it.

Where a tool cannot ask, the policy's ``ask_fallback`` setting (default
``deny``) decides.
"""

from __future__ import annotations

import json
import os
import re

from briefcase.policy.engine import ToolCall

TOOLS = ("claude-code", "cursor", "codex")
NAMES = {"claude-code": "Claude Code", "cursor": "Cursor", "codex": "Codex CLI"}

# Claude Code tool names (https://code.claude.com/docs/en/hooks#pretooluse-input). The Cursor
# names are included because Cursor also runs Claude Code hooks it finds in ~/.claude/settings.json
# (https://cursor.com/docs/reference/third-party-hooks) and sends its own tool names to them.
_SHELL_TOOLS = ("Bash", "Shell")
_READ_TOOLS = ("Read", "Glob", "Grep")
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit", "Delete")
_PATH_KEYS = ("file_path", "notebook_path", "path", "target_file")

# Events that can show the user a question.
_CAN_ASK = {
    "claude-code": ("PreToolUse",),
    "cursor": ("beforeShellExecution", "beforeMCPExecution"),
    "codex": (),
}
CLAUDE_MATCHER = "^(Bash|PowerShell|Read|Write|Edit|MultiEdit|NotebookEdit|Glob|Grep|WebFetch|WebSearch)$|^mcp__"
CODEX_MATCHER = "^(Bash|apply_patch)$|^mcp__"
CURSOR_EVENTS = ("beforeShellExecution", "beforeMCPExecution", "beforeReadFile", "preToolUse")
CURSOR_PRETOOL_MATCHER = "Write|Delete"


def _text(value):
    """The string, or ``None`` for anything that is not a string with something in it (whitespace is nothing)."""
    return value if isinstance(value, str) and value.strip() else None


def _first_path(tool_input):
    if isinstance(tool_input, dict):
        for key in _PATH_KEYS:
            value = _text(tool_input.get(key))
            if value:
                return value
    return None


def _command_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        import shlex
        return " ".join(shlex.quote(item) for item in value)
    return None


def _shell_defect(command, cwd):
    """A command is analysed against its working directory; without a usable one a relative file reference cannot be
    resolved, and the analysis would silently leave it out."""
    if not command.strip():
        return "the command is empty"
    if "\x00" in command:
        return "the command has a NUL character"
    if cwd and "\x00" in cwd:
        return "the working directory has a NUL character"
    if not (cwd and os.path.isabs(cwd)):
        return "no usable working directory in the hook input, so relative file references cannot be judged"
    return None


def _path_defect(paths, cwd):
    """A relative path judged without the event's working directory would be judged against the hook process's own. A
    path with a NUL character cannot name a file at all."""
    if any("\x00" in path for path in paths) or (cwd and "\x00" in cwd):
        return "a path or working directory with a NUL character"
    if cwd and os.path.isabs(cwd):
        return None
    if cwd:
        return "a relative working directory in the hook input"
    for path in paths:
        if not (os.path.isabs(path) or path.startswith("~") or (len(path) > 2 and path[1:3] in (":\\", ":/"))):
            return "a relative path and no working directory in the hook input"
    return None


def _mcp_name(tool_name):
    """``mcp__server__tool`` -> ``(server, tool)``."""
    rest = tool_name[len("mcp__"):]
    server, sep, name = rest.partition("__")
    server, name = (server.strip() or None), (name.strip() or None)
    return (server, name) if sep else (server, None)


def _tool_call(tool, tool_name, tool_input, cwd, common):
    """Shared by the three ``preToolUse``-style events, which all send ``tool_name`` and ``tool_input``."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool_name in _SHELL_TOOLS:
        command = _command_text(tool_input.get("command"))
        where = _text(tool_input.get("working_directory")) or cwd
        if command is None:
            return ToolCall("invalid", tool, tool_name, note="no command in the hook input", **common)
        return ToolCall("shell", tool, tool_name, command=command, cwd=where,
                        defect=_shell_defect(command, where), **common)
    if tool_name == "PowerShell":
        command = _command_text(tool_input.get("command")) or ""
        return ToolCall("shell", tool, tool_name, command=command, cwd=cwd, dialect="powershell",
                        defect=_shell_defect(command, cwd), **common)
    if tool_name in _READ_TOOLS:
        path = _first_path(tool_input) or (cwd if tool_name != "Read" else None)
        if not path:
            return ToolCall("invalid", tool, tool_name, note="no file path in the hook input", **common)
        defect = _path_defect([path], cwd)
        if not defect and tool_name in ("Glob", "Grep") and not _text(tool_input.get("pattern")):
            defect = f"{tool_name} names no pattern, so what it searches for cannot be judged"
        return ToolCall("read", tool, tool_name, paths=[path], cwd=cwd, defect=defect, **common)
    if tool_name in _WRITE_TOOLS:
        path = _first_path(tool_input)
        if not path:
            return ToolCall("invalid", tool, tool_name, note="no file path in the hook input", **common)
        return ToolCall("write", tool, tool_name, paths=[path], cwd=cwd, defect=_path_defect([path], cwd), **common)
    if tool_name == "WebFetch":
        url = _text(tool_input.get("url"))
        return ToolCall("web", tool, tool_name, url=url, cwd=cwd, defect=None if url else "WebFetch names no URL",
                        **common)
    if tool_name == "WebSearch":
        query = _text(tool_input.get("query"))
        return ToolCall("web", tool, tool_name, url=None, cwd=cwd, defect=None if query else "WebSearch names no query",
                        **common)
    if not tool_name:
        return ToolCall("invalid", tool, tool_name, note="the hook input names no tool", **common)
    if tool_name.startswith("mcp__"):
        server, name = _mcp_name(tool_name)
        return ToolCall("mcp", tool, tool_name, server=server, name=name, cwd=cwd,
                        defect=None if (server and name) else "the MCP tool name does not identify a server and a tool",
                        **common)
    if tool_name.startswith("MCP:"):
        return ToolCall("mcp", tool, tool_name, server=None, name=tool_name[4:] or None, cwd=cwd,
                        defect="the MCP tool name does not identify a server", **common)
    return ToolCall("other", tool, tool_name, cwd=cwd, defect=f"a tool this policy does not cover ({tool_name})",
                    **common)


def patch_paths(patch):
    """File paths named in a Codex ``apply_patch`` body.

    The patch grammar is not part of the hooks documentation; this reads the
    ``*** Add File:``, ``*** Update File:``, ``*** Delete File:`` and
    ``*** Move to:`` headers and returns nothing for any other shape."""
    paths = []
    for line in patch.splitlines():
        for marker in ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: "):
            if line.startswith(marker):
                path = line[len(marker):].strip()
                if path and path not in paths:
                    paths.append(path)
    return paths


def parse(tool, payload):
    """The tool call a hook payload describes, checked by :func:`validate` whatever host and event it came from."""
    return validate(_parse(tool, payload))


_FORBIDDEN_HOST = re.compile(r"[\x00-\x20#/:<>?@\[\\\]^|%\x7f]")


def _absolute_url_problem(url):
    """Why ``url`` is not an absolute URL a fetch client would read the way the policy engine does (``None`` when it
    is). The authority must parse, have a well-formed port, and name a host in its canonical form: ASCII, none of the
    code points the URL Standard forbids in a host, no percent-encoding, and a number only as a canonical dotted
    IPv4 address (``0x7f.0.0.1`` and ``2130706433`` are 127.0.0.1 to a client and a different string to a rule). A
    malformed percent escape anywhere is refused, and the host must agree with the one rules are matched against."""
    import ipaddress
    from urllib.parse import urlsplit

    from briefcase.policy.engine import host_of

    text = url.strip()
    if re.search(r"%(?![0-9A-Fa-f]{2})", text):
        return "it has a malformed percent escape"
    try:
        parts = urlsplit(text)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for a port that is not a number in range
    except ValueError:
        return "it does not parse"
    if not parts.scheme or not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*$", parts.scheme):
        return "it has no scheme"
    if not host:
        return "it has no host"
    if re.search(r"[\s\\]", parts.netloc):
        return "its authority has whitespace or a backslash"
    # The raw host-and-port text (after the user information) must be exactly a host and a port, and the parser's
    # reading must be that same host and port: older parsers drop text between `]` and the port, newer ones keep it.
    hostport = parts.netloc.rsplit("@", 1)[-1]
    shape = re.match(r"^\[([0-9A-Fa-f:.]+)\](?::([0-9]*))?$", hostport) or re.match(
        r"^([^:\[\]]*)(?::([0-9]*))?$", hostport)
    if shape is None:
        return "its host and port are not written as a host and a port"
    if shape.group(1).lower() != host or (int(shape.group(2)) if shape.group(2) else None) != parts.port:
        return "the parser read another host or port than the one written"
    if not host.isascii():
        return "its host is not ASCII"
    if "%" in host:
        return "its host is percent-encoded or has a zone identifier"
    if "[" in parts.netloc:
        try:
            address = ipaddress.IPv6Address(host)
        except ValueError:
            return "its IPv6 host is malformed"
        if address.ipv4_mapped is not None or shape.group(1).lower() != address.compressed:
            return "its IPv6 address is not in canonical (compressed, lowercase) form"
    else:
        if _FORBIDDEN_HOST.search(host):
            return "its host has a character a host cannot contain"
        last = host.rstrip(".").rsplit(".", 1)[-1].lower()
        if last.isdigit() or last.startswith("0x"):
            try:
                if str(ipaddress.IPv4Address(host.rstrip("."))) != host.rstrip("."):
                    raise ValueError(host)
            except ValueError:
                return "its host is a number that is not a canonical IPv4 address"
    if host_of(text) != host.rstrip(".").lower():
        return "the policy engine reads another host from it"
    return None


def validate(call):
    """The checks every judged call passes in strict mode, whatever the host and the event: the hook input says where
    it runs (the absolute directory policy is loaded from, ``call.location()``), a URL is absolute with a
    well-formed authority, and a web search has a query. A problem becomes ``call.defect``, which fail-closed mode
    denies."""
    if call.kind in ("invalid", "other") or call.defect:
        return call
    where = call.location()
    if "\x00" in (call.cwd or "") + (call.project_dir or ""):
        call.defect = "a working or project directory with a NUL character"
    elif not (where and os.path.isabs(where)):
        call.defect = "no absolute working directory or project directory in the hook input, so project policy " \
                      "cannot be found"
    elif call.kind == "web" and call.tool_name == "WebFetch":
        problem = _absolute_url_problem(call.url or "")
        if problem:
            call.defect = f"WebFetch names a URL that is not absolute with an authority ({problem})"
    return call


def _parse(tool, payload):
    """The tool call a hook payload describes. A tool this policy does not cover is kind ``other`` (never judged); input
    that is incomplete or not what the host documents is kind ``invalid``, which fail-closed mode denies."""
    if not isinstance(payload, dict):
        return ToolCall("invalid", tool, note="the hook input is not a JSON object")
    event = _text(payload.get("hook_event_name")) or ""
    cwd = _text(payload.get("cwd"))
    if tool == "cursor":
        roots = payload.get("workspace_roots")
        root = roots[0] if isinstance(roots, list) and roots and isinstance(roots[0], str) else None
        project = os.environ.get("CURSOR_PROJECT_DIR") or root or cwd
        common = {"project_dir": project, "session_id": _text(payload.get("conversation_id")),
                  "tool_use_id": _text(payload.get("tool_use_id"))}
        if event == "beforeShellExecution":
            command = _command_text(payload.get("command"))
            if command is None:
                return ToolCall("invalid", tool, event, note="no command in the hook input", **common)
            # A workspace root is where the project is, not where this command runs: relative references need `cwd`.
            return ToolCall("shell", tool, "Shell", command=command, cwd=cwd or project,
                            defect=_shell_defect(command, cwd), **common)
        if event == "beforeMCPExecution":
            server, name = _text(payload.get("mcp_server_name")), _text(payload.get("tool_name"))
            return ToolCall("mcp", tool, "MCP", server=server, name=name, cwd=cwd or project,
                            defect=None if (server and name) else "the MCP call does not name a server and a tool",
                            **common)
        if event == "beforeReadFile":
            path = _text(payload.get("file_path"))
            if not path:
                return ToolCall("invalid", tool, event, note="no file path in the hook input", **common)
            return ToolCall("read", tool, "Read", paths=[path], cwd=cwd or project,
                            defect=_path_defect([path], cwd or project), **common)
        if event == "preToolUse":
            return _tool_call(tool, _text(payload.get("tool_name")) or "", payload.get("tool_input"),
                              cwd or project, common)
        return ToolCall("invalid", tool, event, note=f"an event this hook is not installed for ({event or 'none'})",
                        **common)
    common = {"project_dir": os.environ.get("CLAUDE_PROJECT_DIR") or cwd if tool == "claude-code" else cwd,
              "session_id": _text(payload.get("session_id")), "tool_use_id": _text(payload.get("tool_use_id"))}
    if event and event not in ("PreToolUse", "preToolUse"):
        return ToolCall("invalid", tool, event, note=f"an event this hook is not installed for ({event})", **common)

    call = _claude_codex_call(tool, payload, common)
    if not event and call.kind != "invalid" and not call.defect:
        call.defect = "the hook input names no event"
    return call


def _claude_codex_call(tool, payload, common):
    cwd = _text(payload.get("cwd"))
    tool_name = _text(payload.get("tool_name")) or ""
    tool_input = payload.get("tool_input")
    if tool == "codex" and tool_name == "apply_patch":
        patch = _command_text(tool_input.get("command")) if isinstance(tool_input, dict) else None
        paths = patch_paths(patch or "")
        if not paths:
            return ToolCall("invalid", tool, tool_name, note="no file paths found in the patch", **common)
        return ToolCall("write", tool, tool_name, paths=paths, cwd=cwd, defect=_path_defect(paths, cwd), **common)
    return _tool_call(tool, tool_name, tool_input, cwd, common)


def event_of(tool, payload):
    event = payload.get("hook_event_name") if isinstance(payload, dict) else None
    if isinstance(event, str) and event:
        return event
    return "PreToolUse" if tool != "cursor" else ""


def can_ask(tool, event):
    return event in _CAN_ASK.get(tool, ())


def message(decision, effect, downgraded=False):
    """The text the person sees in the tool."""
    rule = decision.rule_id or "?"
    reason = decision.reason or "No reason was given for this rule."
    head = "Blocked by briefcase-ai policy" if effect == "deny" else "briefcase-ai policy asks you to confirm"
    text = f"{head} (rule {rule}): {reason}"
    if downgraded:
        text += " The rule says ask; this tool cannot ask from a hook, so the policy's ask_fallback applies."
    return text + " Details: briefcase-ai policy log -n 1"


def respond(tool, event, effect, text="", warning=None):
    """``stdout`` for one decision. The process always exits 0; the JSON carries the verdict."""
    if tool == "cursor":
        out = {"permission": effect if effect in ("allow", "deny", "ask") else "allow"}
        if effect != "allow" and text:
            out["user_message"] = text
            if event != "beforeReadFile":
                out["agent_message"] = text
        return json.dumps(out)
    # Claude Code and Codex share the PreToolUse shape.
    if effect in ("deny", "ask"):
        return json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": effect, "permissionDecisionReason": text}})
    if warning:
        return json.dumps({"systemMessage": warning})
    return ""


def fail_open_output(tool):
    """What to print when the hook itself failed and the policy is not fail-closed."""
    return '{"permission": "allow"}' if tool == "cursor" else ""


# Which hook event carries each kind of call, per tool. ``None``: the tool has
# no documented hook for it, so a rule of that kind is not enforced there.
COVERAGE = {
    "claude-code": {"shell": "PreToolUse", "read": "PreToolUse", "write": "PreToolUse", "mcp": "PreToolUse",
                    "web": "PreToolUse"},
    "cursor": {"shell": "beforeShellExecution", "read": "beforeReadFile", "write": "preToolUse",
               "mcp": "beforeMCPExecution", "web": None},
    "codex": {"shell": "PreToolUse", "read": None, "write": "PreToolUse", "mcp": "PreToolUse", "web": None},
}


def effective(tool, kind, effect, ask_fallback):
    """``(effect the tool applies, note)`` for a decision of ``effect`` on a ``kind`` call.
    The effect is ``None`` where the tool has no hook for that kind of call."""
    event = COVERAGE.get(tool, {}).get(kind)
    if event is None:
        if tool == "codex" and kind == "read":
            return None, "no file-read hook; Codex reads files through shell commands, which are checked"
        return None, "no hook for this kind of call; not enforced"
    if effect == "ask" and not can_ask(tool, event):
        return ask_fallback, f"cannot ask from {event}; ask_fallback = {ask_fallback}"
    return effect, ""
