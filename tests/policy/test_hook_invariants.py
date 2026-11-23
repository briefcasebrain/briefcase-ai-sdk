"""Enforcement fails closed: under --fail-closed, no way for the policy or the hook to break lets a call through.

Generated over {policy state} x {tool} x {entry point}: the module's `hook.main`, the SDK's own
`briefcase-ai hook <tool> --enforce` command (the portable launcher), and the isolated launcher script that
`policy init` installs by default. Under --fail-closed each must deny every call (or exit 2, which all three
tools treat as a block) whenever the policy is missing, empty, unreadable, a directory, invalid TOML, or
otherwise cannot be applied; without --fail-closed the same states allow. A deny is judged from the tool's own
documented response shape, not from any phrase the hook prints."""
import io
import json
import os
import subprocess
import sys

import pytest

from briefcase.cli.__main__ import main as cli_main
from briefcase.policy import hook, install

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX paths")

VALID = 'version = 1\npreset = "guarded"\n'
STATES = ["absent", "empty", "invalid_toml", "wrong_schema", "directory", "unreadable", "valid_denying"]
TOOLS = ["claude-code", "cursor", "codex"]
ENTRY = ["module", "module_options_fail", "cli", "cli_broken_import", "launcher", "launcher_broken_import"]
ISOLATED = ["-I"] + (["-S"] if sys.version_info >= (3, 11) else [])


def payload(tool, project):
    if tool == "cursor":
        return {"conversation_id": "c", "generation_id": "g", "model": "m", "hook_event_name": "beforeShellExecution",
                "cursor_version": "1", "workspace_roots": [str(project)], "user_email": "a@example.com",
                "transcript_path": None, "command": "rm -rf /", "cwd": str(project)}
    base = {"session_id": "s", "transcript_path": None, "cwd": str(project), "hook_event_name": "PreToolUse",
            "tool_name": "Bash", "tool_input": {"command": "rm -rf /"}, "tool_use_id": "t"}
    return base


def denies(tool, code, raw):
    """Did the hook block the call, judged from the tool's documented response?"""
    if code == 2:
        return True
    if not raw.strip():
        return False
    out = json.loads(raw)
    if tool == "cursor":
        return out.get("permission") == "deny"
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def build_state(home, project, state):
    path = home / ".briefcase" / "policy.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if state == "empty":
        path.write_text("")
    elif state == "invalid_toml":
        path.write_text("version = \n[[")
    elif state == "wrong_schema":
        path.write_text('version = 1\nnope = true\n[[rule]]\nid = 5\n')
    elif state == "directory":
        path.mkdir()
    elif state == "unreadable":
        path.write_text(VALID)
        path.chmod(0)
    elif state == "valid_denying":
        path.write_text(VALID)
    return path


def run_entry(entry, tool, flags, body, home):
    raw = json.dumps(body)
    if entry in ("module", "module_options_fail"):
        out = io.StringIO()
        real = hook.Options
        if entry == "module_options_fail":  # the option parser itself fails: the command line still decides
            def boom(argv):
                raise RuntimeError("simulated")
            hook.Options = boom
        try:
            code = hook.main([tool, "--enforce", *flags], stdin=io.StringIO(raw), stdout=out)
        finally:
            hook.Options = real
        return code, out.getvalue()
    if entry in ("cli", "cli_broken_import"):
        import contextlib

        out = io.StringIO()
        sys.stdin = io.StringIO(raw)
        real = hook.main
        if entry == "cli_broken_import":  # the hook module cannot do its job (import or internal failure)
            def broken(*args, **kwargs):
                raise ImportError("simulated")
            hook.main = broken
        try:
            with contextlib.redirect_stdout(out):
                code = cli_main(["hook", tool, "--enforce", *flags])
        finally:
            sys.stdin = sys.__stdin__
            hook.main = real
        return code, out.getvalue()
    argv = [sys.executable, *ISOLATED, install.launcher_path(), tool, "--enforce", *flags]
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", ""), "BRIEFCASE_SYSTEM_POLICY": os.environ[
        "BRIEFCASE_SYSTEM_POLICY"]}
    if entry == "launcher_broken_import":
        env["PYTHONPATH"] = ""
        argv = [sys.executable, *ISOLATED, "-c",
                "import sys; sys.modules['briefcase.policy.hook'] = None; "
                "import runpy; sys.argv = ['hook_main.py'] + sys.argv[1:]; "
                "runpy.run_path(%r, run_name='__main__')" % install.launcher_path(),
                tool, "--enforce", *flags]
    done = subprocess.run(argv, input=raw, capture_output=True, text=True, env=env, timeout=60)
    return done.returncode, done.stdout


COMBOS = [(s, t, e, c) for s in STATES for t in TOOLS for e in ENTRY for c in (False, True)]


@pytest.mark.parametrize("state, tool, entry, closed", COMBOS,
                         ids=[f"{s}-{t}-{e}-{'closed' if c else 'open'}" for s, t, e, c in COMBOS])
def test_a_broken_policy_never_lets_a_call_through_under_fail_closed(home, project, state, tool, entry, closed):
    if state == "unreadable" and os.geteuid() == 0:
        pytest.skip("root reads everything")
    if (entry.endswith("broken_import") or entry == "module_options_fail") and state != "valid_denying":
        pytest.skip("one state is enough for an import that cannot work")
    path = build_state(home, project, state)
    try:
        code, raw = run_entry(entry, tool, ["--fail-closed"] if closed else [], payload(tool, project), home)
    finally:
        if state == "unreadable":
            path.chmod(0o600)
    blocked = denies(tool, code, raw)
    broken = entry.endswith("broken_import") or entry == "module_options_fail"
    if state == "valid_denying" and not broken:
        assert blocked, "a valid denying policy must deny"
    if closed:
        assert blocked, f"fail-closed let the call through (exit {code}, output {raw!r})"
    elif state != "valid_denying" or broken:
        assert not blocked and code == 0, "fail-open must allow when the policy cannot be applied"
    if code not in (0, 2):
        pytest.fail(f"exit code {code}: only 0 or 2 are understood by the tools")
    if tool == "cursor" and code == 0:
        assert raw.strip(), "a Cursor hook that prints nothing blocks"


@pytest.mark.parametrize("endpoint, bound, expect", [
    ("http://127.0.0.1:4318", "", True), ("http://localhost:4318", "", True), ("http://[::1]:4318", "", True),
    ("https://evil.example.com", "", False), ("https://evil.example.com", "https://other.example.com", False),
    ("https://rx.example.com", "https://rx.example.com/", True), ("not a url", "", False),
])
def test_the_receiver_token_goes_only_to_the_receiver_it_belongs_to(monkeypatch, endpoint, bound, expect):
    env = {"BRIEFCASE_RECEIVER_TOKEN": "tok", "BRIEFCASE_RECEIVER_TOKEN_ENDPOINT": bound}
    assert (hook.token_for(endpoint, env) == "tok") is expect
    from briefcase.cli import hook_commands

    sent = []
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(hook_commands, "forward", lambda payload, ep, timeout, token=None: sent.append((ep, token)))
    hook_commands.run_cursor(["--endpoint", endpoint], io.StringIO('{"hook_event_name": "stop"}'))
    assert len(sent) == 1 and (sent[0][1] == "tok") is expect
