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
    specific = out.get("hookSpecificOutput", {})
    # The host recognises a deny only in this exact shape: the event name is part of it.
    return specific.get("permissionDecision") == "deny" and specific.get("hookEventName") == "PreToolUse"


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
    ("http://127.0.0.1:4318", "", True), ("http://localhost:4318", "", False), ("http://127.0.0.1:9", "", False),
    ("http://127.0.0.1:9", "http://127.0.0.1:9", True), ("http://127.0.0.1:4318", "http://127.0.0.1:9", False),
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


# --- malformed input: under fail-closed the hook allows only a fully valid event the policy explicitly allows ----

MALFORMED = ["shell_without_command", "non_object_root", "file_event_without_path", "unknown_event",
             "tool_without_name", "empty_object"]


def malformed(tool, kind, project):
    if kind == "non_object_root":
        return ["not", "an", "object"]
    if kind == "empty_object":
        return {}
    if tool == "cursor":
        base = {"conversation_id": "c", "generation_id": "g", "model": "m", "cursor_version": "1",
                "workspace_roots": [str(project)], "user_email": "a@example.com", "transcript_path": None,
                "cwd": str(project)}
        if kind == "shell_without_command":
            return dict(base, hook_event_name="beforeShellExecution")
        if kind == "file_event_without_path":
            return dict(base, hook_event_name="beforeReadFile")
        if kind == "unknown_event":
            return dict(base, hook_event_name="afterFileEdit", command="ls")
        return dict(base, hook_event_name="preToolUse", tool_name="", tool_input={})
    base = {"session_id": "s", "transcript_path": None, "cwd": str(project), "hook_event_name": "PreToolUse",
            "tool_use_id": "t"}
    if kind == "shell_without_command":
        return dict(base, tool_name="Bash", tool_input={})
    if kind == "file_event_without_path":
        if tool == "codex":
            return dict(base, tool_name="apply_patch", tool_input={"command": "no headers here"})
        return dict(base, tool_name="Write", tool_input={})
    if kind == "unknown_event":
        return dict(base, hook_event_name="Stop", tool_name="Bash", tool_input={"command": "ls"})
    return dict(base, tool_name="", tool_input={})


M_COMBOS = [(t, k, e, c, st) for t in TOOLS for k in MALFORMED for e in ("module", "cli", "launcher")
            for c in (False, True) for st in ("valid_denying", "absent")]


@pytest.mark.parametrize("tool, kind, entry, closed, state", M_COMBOS,
                         ids=[f"{t}-{k}-{e}-{'closed' if c else 'open'}-{s}" for t, k, e, c, s in M_COMBOS])
def test_malformed_input_is_denied_under_fail_closed_and_allowed_otherwise(home, project, tool, kind, entry,
                                                                           closed, state):
    build_state(home, project, state)
    code, raw = run_entry(entry, tool, ["--fail-closed"] if closed else [], malformed(tool, kind, project), home)
    blocked = denies(tool, code, raw)
    if closed:
        assert blocked, f"fail-closed let malformed input ({kind}) through: exit {code}, output {raw!r}"
    else:
        assert not blocked and code == 0
        if tool == "cursor":
            assert raw.strip(), "a Cursor hook that prints nothing blocks"


class _Broken(io.StringIO):
    def write(self, text):
        raise OSError("closed pipe")


def test_a_verdict_that_cannot_be_written_blocks_with_the_hosts_status_under_fail_closed(home, project):
    build_state(home, project, "valid_denying")
    for tool in TOOLS:
        code = hook.main([tool, "--enforce", "--fail-closed"], stdin=io.StringIO(json.dumps(payload(tool, project))),
                         stdout=_Broken())
        assert code == 2
        code = hook.main([tool, "--enforce"], stdin=io.StringIO(json.dumps(payload(tool, project))), stdout=_Broken())
        assert code == 0  # not enforcing strictly: unchanged


def test_a_managed_hook_never_falls_back_to_a_weaker_policy(home, project, monkeypatch, tmp_path):
    import hashlib

    system = tmp_path / "system" / "policy.toml"
    system.parent.mkdir()
    strict = 'version = 1\npreset = "strict"\n'
    system.write_text(strict)
    monkeypatch.setenv("BRIEFCASE_SYSTEM_POLICY", str(system))
    required = hashlib.sha256(strict.encode()).hexdigest()
    harmless = {"session_id": "s", "transcript_path": None, "cwd": str(project), "hook_event_name": "PreToolUse",
                "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "t"}
    flags = ["--fail-closed", "--require-policy-sha256", required]
    out = io.StringIO()
    hook.main(["claude-code", "--enforce", *flags], stdin=io.StringIO(json.dumps(harmless)), stdout=out)
    assert not denies("claude-code", 0, out.getvalue()), "the required policy is present and allows ls"
    system.unlink()  # the required file disappears; a valid weaker policy remains
    (home / ".briefcase").mkdir(exist_ok=True)
    (home / ".briefcase" / "policy.toml").write_text(VALID.replace("guarded", "observe"))
    out = io.StringIO()
    hook.main(["claude-code", "--enforce", *flags], stdin=io.StringIO(json.dumps(harmless)), stdout=out)
    assert denies("claude-code", 0, out.getvalue())
    system.write_text(strict + "# changed\n")  # a different file at the same path is not the required one either
    out = io.StringIO()
    hook.main(["claude-code", "--enforce", *flags], stdin=io.StringIO(json.dumps(harmless)), stdout=out)
    assert denies("claude-code", 0, out.getvalue())


def test_fail_closed_is_not_installed_where_the_hook_could_not_start(home, project, monkeypatch, capsys):
    from argparse import Namespace

    from briefcase.cli import policy_commands

    monkeypatch.setattr(install, "launcher_path", lambda: str(home / "missing" / "hook_main.py"))
    lines = []
    code = policy_commands.run_init(Namespace(tools=["claude-code"], preset=None, scope="user", fail_closed=True,
                                              portable=False, capture_content=False, endpoint=None, force=False,
                                              policy_dry_run=False, dry_run=False, yes=True), out=lines.append)
    assert code == 1 and not (home / ".claude" / "settings.json").exists()


# --- round 9: one verdict function, strictness parsed on its own, a status that survives shutdown --------------------

def _call(tool, project, **fields):
    if tool == "cursor":
        base = {"conversation_id": "c", "generation_id": "g", "model": "m", "cursor_version": "1",
                "workspace_roots": [str(project)], "transcript_path": None, "cwd": str(project)}
        return dict(base, **fields)
    base = {"session_id": "s", "transcript_path": None, "cwd": str(project), "tool_use_id": "t"}
    return dict(base, **fields)


def _cases(tool, project):
    """Inputs a strict hook must deny however the policy is written: name a tool it does not cover, a command that is
    empty, an event with no name."""
    if tool == "cursor":
        return {
            "unknown_tool": _call(tool, project, hook_event_name="preToolUse", tool_name="Teleport", tool_input={}),
            "empty_command": _call(tool, project, hook_event_name="beforeShellExecution", command="  "),
            "no_event_name": _call(tool, project, command="ls"),
        }
    return {
        "unknown_tool": _call(tool, project, hook_event_name="PreToolUse", tool_name="Teleport", tool_input={}),
        "empty_command": _call(tool, project, hook_event_name="PreToolUse", tool_name="Bash",
                               tool_input={"command": ""}),
        "no_event_name": _call(tool, project, tool_name="Bash", tool_input={"command": "ls"}),
    }


DEFECTS = ["unknown_tool", "empty_command", "no_event_name"]
D_COMBOS = [(t, k, e, st) for t in TOOLS for k in DEFECTS for e in ("module", "cli", "launcher")
            for st in ("absent", "valid_denying", "allow_everything")]


@pytest.mark.parametrize("tool, kind, entry, state", D_COMBOS,
                         ids=[f"{t}-{k}-{e}-{s}" for t, k, e, s in D_COMBOS])
def test_a_strict_hook_denies_an_unknown_tool_an_empty_command_and_a_missing_event_name(home, project, tool, kind,
                                                                                        entry, state):
    if state == "allow_everything":
        path = home / ".briefcase" / "policy.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('version = 1\npreset = "observe"\n')
    else:
        build_state(home, project, state)
    body = _cases(tool, project)[kind]
    code, raw = run_entry(entry, tool, ["--fail-closed"], body, home)
    assert denies(tool, code, raw), f"fail-closed let {kind} through: exit {code}, output {raw!r}"
    code, raw = run_entry(entry, tool, [], body, home)  # not strict: judged as before
    assert not denies(tool, code, raw) or state == "valid_denying"


def test_a_policy_that_says_fail_closed_also_denies_a_tool_it_does_not_cover(home, project):
    path = home / ".briefcase" / "policy.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('version = 1\npreset = "observe"\nfail_closed = true\n')
    body = _cases("claude-code", project)["unknown_tool"]
    code, raw = run_entry("module", "claude-code", [], body, home)  # no flag on the command line
    assert denies("claude-code", code, raw)


@pytest.mark.parametrize("flags", [["--endpoint", "--fail-closed"], ["--require-policy-sha256", "--fail-closed"],
                                   ["--fail-closed", "--endpoint"],
                                   ["--endpoint", "--fail-closed", "--capture-content"]])
@pytest.mark.parametrize("tool", TOOLS)
def test_an_option_without_a_value_cannot_swallow_fail_closed(home, project, tool, flags):
    build_state(home, project, "absent")  # fail open would allow
    out = io.StringIO()
    code = hook.main([tool, "--enforce", *flags], stdin=io.StringIO(json.dumps(payload(tool, project))), stdout=out)
    assert denies(tool, code, out.getvalue()), (code, out.getvalue())


def _closed_pipe_run(argv, body, env):
    """Run a hook whose stdout is a pipe nobody reads, so the verdict cannot be written."""
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    child.stdout.close()
    try:
        child.stdin.write(body.encode())
        child.stdin.close()
    except BrokenPipeError:
        pass
    child.wait(timeout=60)
    stderr = child.stderr.read().decode()
    child.stderr.close()
    return child.returncode, stderr


@pytest.mark.parametrize("closed", [True, False])
@pytest.mark.parametrize("entry", ["launcher", "module"])
def test_the_blocking_status_survives_interpreter_shutdown_on_a_broken_pipe(home, project, entry, closed):
    """Python retries the failed flush at exit and replaces the status with 120; the hosts treat only 2 as a block."""
    build_state(home, project, "valid_denying")
    flags = ["--fail-closed"] if closed else []
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", ""),
           "BRIEFCASE_SYSTEM_POLICY": os.environ["BRIEFCASE_SYSTEM_POLICY"]}
    if entry == "launcher":
        argv = [sys.executable, *ISOLATED, install.launcher_path(), "claude-code", "--enforce", *flags]
    else:
        root = os.path.dirname(os.path.dirname(os.path.abspath(install.launcher_path())))
        script = ("import sys, types; m = types.ModuleType('briefcase'); m.__path__ = [%r]; "
                  "sys.modules['briefcase'] = m; from briefcase.policy import hook; "
                  "sys.exit(hook.main(sys.argv[1:]))" % root)
        argv = [sys.executable, *ISOLATED, "-c", script, "claude-code", "--enforce", *flags]
    code, stderr = _closed_pipe_run(argv, json.dumps(payload("claude-code", project)), env)
    assert code == (2 if closed else 0), (code, stderr)


def test_the_launcher_fallbacks_do_not_lose_their_status_on_a_broken_pipe(home, project, tmp_path):
    """The launcher's own deny-on-import-failure path writes to stderr/stdout too."""
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
    argv = [sys.executable, *ISOLATED, "-c",
            "import sys; sys.modules['briefcase.policy.hook'] = None; "
            "import runpy; sys.argv = ['hook_main.py'] + sys.argv[1:]; "
            "runpy.run_path(%r, run_name='__main__')" % install.launcher_path(),
            "cursor", "--enforce", "--fail-closed"]
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    child.stderr.close()
    child.stdout.close()
    child.stdin.close()
    child.wait(timeout=60)
    assert child.returncode == 2


class _SwapAfterFirstRead(object):
    """Open the system policy normally, but replace the file the moment it has been opened once."""

    def __init__(self, path, replacement, real_open):
        self.path, self.replacement, self.real_open, self.opened = str(path), replacement, real_open, 0

    def __call__(self, file, *args, **kwargs):
        handle = self.real_open(file, *args, **kwargs)
        if str(file) == self.path:
            self.opened += 1
            if self.opened == 1:
                data = handle.read()
                handle.close()
                with self.real_open(self.path, "w") as out:
                    out.write(self.replacement)
                return io.BytesIO(data) if "b" in (args[0] if args else kwargs.get("mode", "r")) else io.StringIO(
                    data.decode() if isinstance(data, bytes) else data)
        return handle


def test_the_required_policy_is_judged_from_the_bytes_that_were_hashed(home, project, monkeypatch, tmp_path):
    import builtins
    import hashlib

    system = tmp_path / "system" / "policy.toml"
    system.parent.mkdir()
    strict = 'version = 1\npreset = "guarded"\ndefault = "deny"\n'
    system.write_text('version = 1\npreset = "observe"\n')  # a weak policy is in place when the hook first looks
    monkeypatch.setenv("BRIEFCASE_SYSTEM_POLICY", str(system))
    required = hashlib.sha256(strict.encode()).hexdigest()
    harmless = dict(payload("claude-code", project), tool_input={"command": "ls"})
    # ... and the required file replaces it right after that first read. Whatever is judged must be what was hashed.
    swap = _SwapAfterFirstRead(system, strict, builtins.open)
    monkeypatch.setattr(builtins, "open", swap)
    out = io.StringIO()
    hook.main(["claude-code", "--enforce", "--require-policy-sha256", required],
              stdin=io.StringIO(json.dumps(harmless)), stdout=out)
    assert swap.opened >= 1 and system.read_text() == strict
    assert denies("claude-code", 0, out.getvalue()), "a policy other than the authenticated bytes was judged"


@pytest.mark.parametrize("tool", TOOLS)
def test_a_required_policy_mismatch_denies_even_without_fail_closed(home, project, tool, monkeypatch, tmp_path):
    system = tmp_path / "gone" / "policy.toml"
    monkeypatch.setenv("BRIEFCASE_SYSTEM_POLICY", str(system))
    (home / ".briefcase").mkdir(exist_ok=True)
    (home / ".briefcase" / "policy.toml").write_text('version = 1\npreset = "observe"\n')
    harmless = dict(payload(tool, project))
    harmless["command" if tool == "cursor" else "tool_input"] = "ls" if tool == "cursor" else {"command": "ls"}
    out = io.StringIO()
    code = hook.main([tool, "--enforce", "--require-policy-sha256", "0" * 64],
                     stdin=io.StringIO(json.dumps(harmless)), stdout=out)
    assert denies(tool, code, out.getvalue())


# --- round 10: a call the policy cannot fully identify is not judged by guesswork ------------------------------------

def _more_defects(tool, project):
    if tool == "cursor":
        return {
            "webfetch_without_url": _call(tool, project, hook_event_name="preToolUse", tool_name="WebFetch",
                                          tool_input={}),
            "mcp_without_tool": _call(tool, project, hook_event_name="beforeMCPExecution", mcp_server_name="srv"),
            "relative_path_no_cwd": _call(tool, project, hook_event_name="beforeReadFile", file_path="notes.txt",
                                          cwd=None, workspace_roots=[]),
        }
    base = dict(hook_event_name="PreToolUse")
    return {
        "webfetch_without_url": _call(tool, project, tool_name="WebFetch", tool_input={}, **base),
        "mcp_without_tool": _call(tool, project, tool_name="mcp__srv", tool_input={}, **base),
        "relative_path_no_cwd": _call(tool, project, tool_name="Write", tool_input={"file_path": "notes.txt"},
                                      **dict(base, cwd=None)),
    }


MORE = ["webfetch_without_url", "mcp_without_tool", "relative_path_no_cwd"]
MORE_COMBOS = [(t, k, e) for t in TOOLS for k in MORE for e in ("module", "cli", "launcher")
               if not (t == "codex" and k == "webfetch_without_url")]


@pytest.mark.parametrize("tool, kind, entry", MORE_COMBOS, ids=[f"{t}-{k}-{e}" for t, k, e in MORE_COMBOS])
def test_a_strict_hook_denies_calls_it_cannot_fully_identify(home, project, tool, kind, entry):
    (home / ".briefcase").mkdir(exist_ok=True)
    (home / ".briefcase" / "policy.toml").write_text('version = 1\npreset = "observe"\n')  # allows what it judges
    body = _more_defects(tool, project)[kind]
    code, raw = run_entry(entry, tool, ["--fail-closed"], body, home)
    assert denies(tool, code, raw), f"fail-closed let {kind} through: exit {code}, output {raw!r}"


@pytest.mark.parametrize("tool", TOOLS)
def test_a_nonempty_shell_command_that_yields_nothing_to_judge_is_denied_when_strict(home, project, tool):
    (home / ".briefcase").mkdir(exist_ok=True)
    (home / ".briefcase" / "policy.toml").write_text('version = 1\npreset = "guarded"\ndefault = "deny"\n')
    body = payload(tool, project)
    body["command" if tool == "cursor" else "tool_input"] = "# nothing but a comment" if tool == "cursor" else {
        "command": "# nothing but a comment"}
    code, raw = run_entry("module", tool, ["--fail-closed"], body, home)
    assert denies(tool, code, raw), (code, raw)


@pytest.mark.parametrize("tool", TOOLS)
def test_a_required_policy_mismatch_is_binding_even_when_bookkeeping_and_output_fail(
        home, project, tool, monkeypatch, tmp_path):
    monkeypatch.setenv("BRIEFCASE_SYSTEM_POLICY", str(tmp_path / "gone" / "policy.toml"))
    flags = ["--require-policy-sha256", "0" * 64]  # no --fail-closed: the hash alone binds the hook
    body = payload(tool, project)

    def boom(*args, **kwargs):
        raise RuntimeError("health file is unwritable")

    monkeypatch.setattr(hook, "record_problem", boom)
    out = io.StringIO()
    code = hook.main([tool, "--enforce", *flags], stdin=io.StringIO(json.dumps(body)), stdout=out)
    assert denies(tool, code, out.getvalue()), "bookkeeping failed and the call went through"
    code = hook.main([tool, "--enforce", *flags], stdin=io.StringIO(json.dumps(body)), stdout=_Broken())
    assert code == 2, "the denial could not be written and the call went through"


def test_with_stdout_and_stderr_both_closed_the_blocking_status_is_still_2(home, project):
    build_state(home, project, "valid_denying")
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", ""),
           "BRIEFCASE_SYSTEM_POLICY": os.environ["BRIEFCASE_SYSTEM_POLICY"]}
    argv = [sys.executable, *ISOLATED, install.launcher_path(), "codex", "--enforce", "--fail-closed"]
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    child.stdout.close()
    child.stderr.close()
    child.stdin.write(json.dumps(payload("codex", project)).encode())
    child.stdin.close()
    child.wait(timeout=60)
    assert child.returncode == 2


def test_when_only_stdout_fails_the_reason_is_a_nonempty_stderr_line(home, project, capsys):
    build_state(home, project, "valid_denying")
    code = hook.main(["codex", "--enforce", "--fail-closed"],
                     stdin=io.StringIO(json.dumps(payload("codex", project))), stdout=_Broken())
    assert code == 2 and capsys.readouterr().err.strip().startswith("Blocked by briefcase-ai policy")


@pytest.mark.parametrize("closed", [True, False])
def test_the_isolated_launcher_survives_a_failure_after_a_successful_import(home, project, closed):
    """A late failure (the hook module imported, then raised): exit 2 when strict, 0 otherwise."""
    script = ("import sys, types\n"
              "root = types.ModuleType('briefcase'); root.__path__ = []\n"
              "policy = types.ModuleType('briefcase.policy'); policy.__path__ = []\n"
              "hook = types.ModuleType('briefcase.policy.hook')\n"
              "def main(argv):\n    raise RuntimeError('late import failed')\n"
              "hook.main = main; policy.hook = hook\n"
              "sys.modules.update({'briefcase': root, 'briefcase.policy': policy, 'briefcase.policy.hook': hook})\n"
              "import runpy; sys.argv = ['hook_main.py'] + sys.argv[1:]\n"
              "runpy.run_path(%r, run_name='__main__')\n" % install.launcher_path())
    argv = [sys.executable, *ISOLATED, "-c", script, "claude-code", "--enforce"] + (["--fail-closed"] if closed else [])
    done = subprocess.run(argv, input="{}", capture_output=True, text=True, timeout=60,
                          env={"HOME": str(home), "PATH": os.environ.get("PATH", "")})
    assert done.returncode == (2 if closed else 0), (done.returncode, done.stderr)


# --- round 11 --------------------------------------------------------------------------------

def _round11(tool, project):
    if tool == "cursor":
        return {
            "blank_url": _call(tool, project, hook_event_name="preToolUse", tool_name="WebFetch",
                               tool_input={"url": "   "}),
            "blank_mcp_server": _call(tool, project, hook_event_name="beforeMCPExecution", mcp_server_name="  ",
                                      tool_name="run"),
            "relative_cwd": _call(tool, project, hook_event_name="beforeReadFile", file_path="notes.txt",
                                  cwd="sub/dir", workspace_roots=[]),
            "shell_without_cwd": _call(tool, project, hook_event_name="beforeShellExecution",
                                       command="cat secret.txt", cwd=None, workspace_roots=[]),
        }
    base = dict(hook_event_name="PreToolUse")
    return {
        "blank_url": _call(tool, project, tool_name="WebFetch", tool_input={"url": "  "}, **base),
        "blank_mcp_server": _call(tool, project, tool_name="mcp__ __run", tool_input={}, **base),
        "relative_cwd": _call(tool, project, tool_name="Write", tool_input={"file_path": "notes.txt"},
                              **dict(base, cwd="sub/dir")),
        "shell_without_cwd": _call(tool, project, tool_name="Bash", tool_input={"command": "cat secret.txt"},
                                   **dict(base, cwd=None)),
    }


R11 = ["blank_url", "blank_mcp_server", "relative_cwd", "shell_without_cwd"]
R11_COMBOS = [(t, k, e) for t in TOOLS for k in R11 for e in ("module", "cli", "launcher")
              if not (t == "codex" and k == "blank_url")]


@pytest.mark.parametrize("tool, kind, entry", R11_COMBOS, ids=[f"{t}-{k}-{e}" for t, k, e in R11_COMBOS])
def test_a_strict_hook_denies_blank_identities_a_relative_cwd_and_a_shell_without_one(
        home, project, tool, kind, entry):
    (home / ".briefcase").mkdir(exist_ok=True)
    (home / ".briefcase" / "policy.toml").write_text('version = 1\npreset = "observe"\n')
    code, raw = run_entry(entry, tool, ["--fail-closed"], _round11(tool, project)[kind], home)
    assert denies(tool, code, raw), f"fail-closed let {kind} through: exit {code}, output {raw!r}"


@pytest.mark.parametrize("entry", ["cli_broken_import", "launcher_broken_import"])
@pytest.mark.parametrize("tool", TOOLS)
def test_a_hash_only_managed_hook_blocks_when_the_outer_handlers_catch_a_failure(home, project, tool, entry):
    build_state(home, project, "valid_denying")
    flags = ["--require-policy-sha256", "0" * 64]  # no --fail-closed
    code, raw = run_entry(entry, tool, flags, payload(tool, project), home)
    assert denies(tool, code, raw), (code, raw)


@pytest.mark.parametrize("digest", ["", "   ", "xyz", "0" * 63, "G" * 64])
@pytest.mark.parametrize("tool", TOOLS)
def test_a_required_hash_that_is_not_a_digest_is_invalid_and_denies(home, project, tool, digest):
    (home / ".briefcase").mkdir(exist_ok=True)
    (home / ".briefcase" / "policy.toml").write_text('version = 1\npreset = "observe"\n')
    out = io.StringIO()
    code = hook.main([tool, "--enforce", "--require-policy-sha256", digest],
                     stdin=io.StringIO(json.dumps(payload(tool, project))), stdout=out)
    assert denies(tool, code, out.getvalue()), "an unusable required hash skipped authentication"


def test_the_codex_acknowledgement_states_exactly_what_is_and_is_not_covered():
    text = install.CODEX_STARTUP_RISK
    assert "every failure after the hook starts" not in text
    for covered in ("unreadable policy", "malformed input", "missing required policy", "exits 2"):
        assert covered in text
    for uncovered in ("a missing executable", "a host timeout", "stdout and stderr both being closed"):
        assert "does not cover" in text and uncovered in text
