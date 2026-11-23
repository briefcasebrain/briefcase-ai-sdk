"""``briefcase-ai hook cursor``: forward a Cursor hook event to the receiver.

Cursor runs this for each configured hook with the event as JSON on stdin.
The contract is to never get in the agent's way: it always exits 0, prints
nothing (Cursor reads hook stdout as a JSON verdict, and exit code 2 blocks the
action), and gives up after a short timeout when no receiver is listening.

Content never leaves this process unless the hook was installed with
``--capture-content``: prompt text, agent text, tool input/output and
``user_email`` are dropped here, before the POST.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_ENDPOINT = "http://127.0.0.1:4318"
HOOK_PATH = "/v1/hooks/cursor"
MAX_STDIN_BYTES = 4 * 1024 * 1024
# Dropped unless --capture-content. Lengths of the text fields are kept.
CONTENT_FIELDS = ("prompt", "text", "tool_input", "tool_output", "attachments", "agent_message",
                  "error_message", "user_email", "transcript_path", "workspace_roots", "cwd")
_LENGTH_FIELDS = ("prompt", "text")

GROUP = "connect"


def scrub(payload: Dict[str, Any], capture_content: bool) -> Dict[str, Any]:
    if capture_content:
        return payload
    out = {key: value for key, value in payload.items() if key not in CONTENT_FIELDS}
    for key in _LENGTH_FIELDS:
        if isinstance(payload.get(key), str):
            out[f"{key}_length"] = len(payload[key])
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the Authorization header to another origin: never follow one."""

    def redirect_request(self, *args, **kwargs):
        return None


def forward(payload: Dict[str, Any], endpoint: str, timeout: float, token: Optional[str] = None) -> bool:
    request = urllib.request.Request(
        endpoint.rstrip("/") + HOOK_PATH, data=json.dumps(payload, default=str).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    # No proxy: the receiver is local, and an HTTP proxy from the environment
    # must not see the event.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        return 200 <= response.status < 300


def parse_args(argv: List[str]):
    """Hand-rolled so a bad or unknown flag is ignored: argparse would print a
    usage error and exit 2, which Cursor treats as "block this action"."""
    endpoint = os.environ.get("BRIEFCASE_RECEIVER_ENDPOINT", DEFAULT_ENDPOINT)
    capture_content, timeout = False, 1.0
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag == "--capture-content":
            capture_content = True
        elif flag == "--endpoint" and index + 1 < len(argv):
            index += 1
            endpoint = argv[index]
        elif flag == "--timeout" and index + 1 < len(argv):
            index += 1
            try:
                timeout = min(max(float(argv[index]), 0.05), 10.0)
            except ValueError:
                pass
        index += 1
    return endpoint, capture_content, timeout


def run_enforce(tool: str, argv: List[str], stdin: Any = None, stdout: Any = None) -> int:
    """``briefcase-ai hook <tool> --enforce``: decide one tool call under the policy.

    Always returns 0; the verdict is the JSON on stdout. Fails open unless the
    policy or the command line says ``fail_closed`` (see ``briefcase.policy.hook``)."""
    try:
        from briefcase.policy import hook
        return hook.main([tool] + list(argv), stdin=stdin, stdout=stdout)
    except BaseException:  # noqa: B036 - the policy code could not even be imported
        if "--fail-closed" in argv:  # the one thing the command line promised: nothing runs
            reason = ("Blocked by briefcase-ai policy: the hook could not run and it was installed with "
                      "--fail-closed. Reinstall with `briefcase-ai policy init`, or turn enforcement off with "
                      "`briefcase-ai policy off`.")
            if tool == "cursor":
                print(json.dumps({"permission": "deny", "user_message": reason, "agent_message": reason}))
            else:
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                         "permissionDecisionReason": reason}}))
            return 0
        if tool == "cursor":
            print('{"permission": "allow"}')   # a Cursor permission hook that prints nothing blocks
        return 0


def run_cursor(argv: List[str], stdin: Any = None) -> int:
    """Always returns 0 and writes nothing to stdout (except with ``--enforce``)."""
    if "--enforce" in argv:
        return run_enforce("cursor", argv, stdin)
    try:
        endpoint, capture_content, timeout = parse_args(argv)
        stream = stdin if stdin is not None else sys.stdin
        raw = stream.buffer.read(MAX_STDIN_BYTES + 1) if hasattr(stream, "buffer") else stream.read(MAX_STDIN_BYTES + 1)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if len(raw) > MAX_STDIN_BYTES:
            return 0
        payload = json.loads(raw)
        if isinstance(payload, dict):
            from briefcase.policy.hook import token_for

            forward(scrub(payload, capture_content), endpoint, timeout, token_for(endpoint))
    except BaseException:  # noqa: B036 - fail open on everything, including SystemExit from argparse
        pass
    return 0


def cmd_hook_cursor(args, store=None, engine=None, stack=None) -> int:
    return run_cursor(getattr(args, "hook_args", []) or [])


def cmd_hook_enforce(args, store=None, engine=None, stack=None) -> int:
    return run_enforce(args.hook_tool, getattr(args, "hook_args", []) or [])


def register(sub) -> None:
    hook = sub.add_parser("hook", help="Tool hook entry points (installed by `briefcase-ai connect`).")
    hooks = hook.add_subparsers(dest="sub")
    cursor = hooks.add_parser(
        "cursor", add_help=False,
        help="Forward a Cursor hook event from stdin to the receiver; always exits 0.",
    )
    cursor.add_argument("hook_args", nargs=argparse.REMAINDER)
    cursor.set_defaults(func=cmd_hook_cursor)
    for tool in ("claude-code", "codex"):
        # prefix_chars is a character no flag starts with, so every argument is taken as a
        # positional. argparse can then never reject one and exit 2, which these tools read
        # as "block this tool call".
        enforce = hooks.add_parser(
            tool, add_help=False, prefix_chars="\x00",
            help=f"Decide a {tool} tool call under the briefcase-ai policy (stdin JSON); always exits 0.",
        )
        enforce.add_argument("hook_args", nargs="*")
        enforce.set_defaults(func=cmd_hook_enforce, hook_tool=tool)
