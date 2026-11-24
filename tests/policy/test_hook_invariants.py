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


LAST_STDERR = None  # what the last subprocess entry wrote to stderr (None: not captured)


def denies(tool, code, raw):
    """Did the hook block the call, judged from the tool's documented response? Exit 2 blocks, but Codex needs a
    nonempty stderr reason with it, so a captured empty stderr is not a block there."""
    if code == 2:
        return not (tool == "codex" and LAST_STDERR is not None and not LAST_STDERR.strip())
    if code != 0:  # the hosts read stdout only on exit 0; any other status is a failed hook that lets the call run
        return False
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


def _failing_decide(*args, **kwargs):
    raise RuntimeError("simulated evaluation failure")


def run_entry(entry, tool, flags, body, home):
    global LAST_STDERR
    LAST_STDERR = None
    raw = json.dumps(body)
    if entry in ("module_decide_error", "cli_decide_error"):  # the hook fails after it has read the options
        real_decide = hook.decide
        hook.decide = _failing_decide
        try:
            return run_entry(entry.split("_")[0], tool, flags, body, home)
        finally:
            hook.decide = real_decide
    if entry == "module_write_error":
        class Unflushable(_Broken):  # an allow writes nothing, so the failure has to be in the flush
            def flush(self):
                raise OSError("closed pipe")

        code = hook.main([tool, "--enforce", *flags], stdin=io.StringIO(raw), stdout=Unflushable())
        return code, ""
    if entry == "launcher_late_error":
        script = LATE + ("import runpy; sys.argv = ['hook_main.py'] + sys.argv[1:]\n"
                         "runpy.run_path(%r, run_name='__main__')\n" % install.launcher_path())
        done = subprocess.run([sys.executable, *ISOLATED, "-c", script, tool, "--enforce", *flags], input=raw,
                              capture_output=True, text=True, timeout=60,
                              env={"HOME": str(home), "PATH": os.environ.get("PATH", "")})
        LAST_STDERR = done.stderr
        return done.returncode, done.stdout
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
        import briefcase.policy as policy_pkg

        saved = (policy_pkg.__dict__.pop("hook", None), sys.modules.get("briefcase.policy.hook"))
        if entry == "cli_broken_import":  # the hook module cannot be imported: the import statement itself fails
            sys.modules["briefcase.policy.hook"] = None
        else:
            policy_pkg.hook = saved[0]
        try:
            with contextlib.redirect_stdout(out):
                code = cli_main(["hook", tool, "--enforce", *flags])
        finally:
            sys.stdin = sys.__stdin__
            policy_pkg.hook = saved[0]
            sys.modules["briefcase.policy.hook"] = saved[1]
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
    LAST_STDERR = done.stderr
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
    strict = 'version = 1\npreset = "strict"\ndefault = "allow"\n'  # a bound hook allows only what the policy states
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


# --- round 12 --------------------------------------------------------------------------------

PATCH = "*** Begin Patch\n*** Add File: /tmp/briefcase-round12.txt\n+x\n*** End Patch\n"


def _round12(tool, project):
    if tool == "cursor":
        return {
            "url_without_authority": _call(tool, project, hook_event_name="preToolUse", tool_name="WebFetch",
                                           tool_input={"url": "https://"}),
            "shell_with_only_a_workspace_root": _call(tool, project, hook_event_name="beforeShellExecution",
                                                      command="cat secret.txt", cwd=None),
        }
    base = dict(hook_event_name="PreToolUse")
    return {
        "url_without_authority": _call(tool, project, tool_name="WebFetch", tool_input={"url": "https://"}, **base),
        "patch_without_cwd": _call(tool, project, tool_name="apply_patch", tool_input={"command": PATCH},
                                   **dict(base, cwd=None)),
    }


R12 = [(t, k) for t in TOOLS for k in ("url_without_authority", "patch_without_cwd", "shell_with_only_a_workspace_root")
       if (t, k) not in {("codex", "url_without_authority"), ("claude-code", "shell_with_only_a_workspace_root"),
                         ("codex", "shell_with_only_a_workspace_root"), ("cursor", "patch_without_cwd"),
                         ("claude-code", "patch_without_cwd")}]
R12_COMBOS = [(t, k, e) for t, k in R12 for e in ("module", "cli", "launcher")]


@pytest.mark.parametrize("tool, kind, entry", R12_COMBOS, ids=[f"{t}-{k}-{e}" for t, k, e in R12_COMBOS])
def test_a_strict_hook_denies_a_url_with_no_authority_and_a_call_whose_project_cannot_be_located(
        home, project, tool, kind, entry, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    (home / ".briefcase").mkdir(exist_ok=True)
    (home / ".briefcase" / "policy.toml").write_text('version = 1\npreset = "observe"\n')
    code, raw = run_entry(entry, tool, ["--fail-closed"], _round12(tool, project)[kind], home)
    assert denies(tool, code, raw), f"fail-closed let {kind} through: exit {code}, output {raw!r}"


HASH_FLAGS = [["--require-policy-sha256", "0" * 64], ["--require-policy-sha256=" + "0" * 64]]


@pytest.mark.parametrize("entry", ["module", "cli", "launcher"])
@pytest.mark.parametrize("flags", HASH_FLAGS, ids=["separate", "equals"])
@pytest.mark.parametrize("tool", TOOLS)
def test_a_required_policy_hash_binds_in_both_spellings(home, project, tool, flags, entry):
    (home / ".briefcase").mkdir(exist_ok=True)
    (home / ".briefcase" / "policy.toml").write_text('version = 1\npreset = "observe"\n')  # allows the call
    for extra in ([], ["--fail-closed"]):
        code, raw = run_entry(entry, tool, flags + extra, payload(tool, project), home)
        assert denies(tool, code, raw), f"{flags + extra} did not bind the hook to the policy hash: {code} {raw!r}"


@pytest.mark.parametrize("tool", TOOLS)
def test_the_portable_command_blocks_when_building_the_cli_parser_fails(home, project, tool, monkeypatch):
    import briefcase.cli.__main__ as cli_main_module

    def boom():
        raise RuntimeError("simulated parser failure")

    monkeypatch.setattr(cli_main_module, "build_parser", boom)
    build_state(home, project, "valid_denying")  # the call is one the policy blocks
    code, raw = run_entry("cli", tool, ["--fail-closed"], payload(tool, project), home)
    assert denies(tool, code, raw), (code, raw)


def test_the_broken_import_fixture_is_a_real_import_failure(home, project):
    """The fixture must fail at the import statement, not replace an already imported function."""
    import briefcase.policy as policy_pkg
    saved = (policy_pkg.__dict__.pop("hook", None), sys.modules.get("briefcase.policy.hook"))
    sys.modules["briefcase.policy.hook"] = None
    try:
        with pytest.raises(ImportError):
            from briefcase.policy import hook as _hook  # noqa: F401
    finally:
        policy_pkg.hook = saved[0]
        sys.modules["briefcase.policy.hook"] = saved[1]


# --- verdict delivery ------------------------------------------------------------------------

LATE = ("import sys, types\n"
        "root = types.ModuleType('briefcase'); root.__path__ = []\n"
        "policy = types.ModuleType('briefcase.policy'); policy.__path__ = []\n"
        "hook = types.ModuleType('briefcase.policy.hook')\n"
        "def main(argv):\n    raise RuntimeError('late failure')\n"
        "hook.main = main; policy.hook = hook\n"
        "sys.modules.update({'briefcase': root, 'briefcase.policy': policy, 'briefcase.policy.hook': hook})\n")
BROKEN = "import sys; sys.modules['briefcase.policy.hook'] = None\n"
NO_JSON = "import sys; sys.modules['json'] = None; sys.modules['briefcase.policy.hook'] = None\n"


@pytest.mark.parametrize("flags", [["--fail-closed"], ["--require-policy-sha256", "0" * 64]], ids=["flag", "hash"])
@pytest.mark.parametrize("scenario", [LATE, BROKEN, NO_JSON], ids=["late_failure", "import_failure", "json_broken"])
@pytest.mark.parametrize("tool", TOOLS)
def test_a_launcher_failure_with_stderr_unusable_still_delivers_a_denial_on_stdout(
        home, project, tool, scenario, flags):
    script = scenario + ("import runpy; sys.argv = ['hook_main.py'] + sys.argv[1:]\n"
                         "runpy.run_path(%r, run_name='__main__')\n" % install.launcher_path())
    argv = [sys.executable, *ISOLATED, "-c", script, tool, "--enforce", *flags]
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env={"HOME": str(home), "PATH": os.environ.get("PATH", "")})
    child.stderr.close()
    child.stdin.write(json.dumps(payload(tool, project)).encode())
    child.stdin.close()
    raw = child.stdout.read().decode()
    child.stdout.close()
    child.wait(timeout=60)
    assert child.returncode == 0 and raw.strip(), (child.returncode, raw)
    assert denies(tool, child.returncode, raw)


def test_the_oracle_does_not_accept_a_bare_exit_2_where_codex_needs_a_reason(monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "LAST_STDERR", "")
    assert not denies("codex", 2, "")
    monkeypatch.setattr(sys.modules[__name__], "LAST_STDERR", "Blocked by briefcase-ai policy")
    assert denies("codex", 2, "")
    assert denies("claude-code", 2, "")


# --- class A: one option normaliser for every entry and every handler ------------------------------------------

OPTION_CASES = {  # id: (flags, binds the hook to a required policy, a malformed option)
    "hash_sep_valid": (["--require-policy-sha256", "0" * 64], True, False),
    "hash_eq_valid": (["--require-policy-sha256=" + "0" * 64], True, False),
    "hash_sep_empty": (["--require-policy-sha256", ""], True, True),
    "hash_eq_empty": (["--require-policy-sha256="], True, True),
    "endpoint_sep_valid": (["--endpoint", "http://127.0.0.1:9"], False, False),
    "endpoint_eq_valid": (["--endpoint=http://127.0.0.1:9"], False, False),
    "endpoint_sep_empty": (["--endpoint", ""], False, True),
    "endpoint_eq_empty": (["--endpoint="], False, True),
}
NORMAL = ["module", "cli", "launcher"]
FAILING = ["module_decide_error", "module_write_error", "cli_decide_error", "cli_broken_import",
           "launcher_late_error", "launcher_broken_import"]
OPT_COMBOS = [(c, e, fc) for c in OPTION_CASES for e in NORMAL + FAILING for fc in (False, True)]


@pytest.mark.parametrize("case, entry, fail_closed", OPT_COMBOS,
                         ids=[f"{c}-{e}-{'closed' if f else 'open'}" for c, e, f in OPT_COMBOS])
def test_every_option_in_both_spellings_gives_one_strictness_answer_on_every_handler_path(
        home, project, case, entry, fail_closed):
    flags, binds, malformed = OPTION_CASES[case]
    flags = flags + (["--fail-closed"] if fail_closed else [])
    (home / ".briefcase").mkdir(exist_ok=True)
    (home / ".briefcase" / "policy.toml").write_text('version = 1\npreset = "observe"\n')  # allows the call
    blocked = binds or fail_closed
    expected = blocked if entry in FAILING else (binds or (malformed and fail_closed))
    for tool in TOOLS:
        code, raw = run_entry(entry, tool, flags, payload(tool, project), home)
        assert denies(tool, code, raw) == expected, (tool, flags, entry, code, raw)


@pytest.mark.parametrize("flag", ["--endpoint=", "--endpoint"])
def test_an_empty_endpoint_never_replaces_the_inherited_receiver_or_marks_it_configured(monkeypatch, flag):
    monkeypatch.setenv("BRIEFCASE_RECEIVER_ENDPOINT", "http://receiver.example:4318")
    options = hook.Options(["claude-code", "--enforce", flag] + ([""] if flag == "--endpoint" else []))
    assert options.parse_error
    assert options.endpoint == "http://receiver.example:4318" and options.endpoint_set
    monkeypatch.delenv("BRIEFCASE_RECEIVER_ENDPOINT")
    options = hook.Options(["claude-code", "--enforce", flag] + ([""] if flag == "--endpoint" else []))
    assert not options.endpoint_set


# --- class D: the portable dispatch blocks even when the dispatcher cannot be imported -------------------------

@pytest.mark.parametrize("flags", [["--fail-closed"], ["--require-policy-sha256=" + "0" * 64], []],
                         ids=["flag", "hash", "open"])
@pytest.mark.parametrize("tool", TOOLS)
def test_the_portable_command_blocks_when_the_dispatcher_module_cannot_be_imported(
        home, project, tool, flags, monkeypatch):
    import briefcase.cli as cli_pkg

    build_state(home, project, "valid_denying")
    monkeypatch.setitem(sys.modules, "briefcase.cli.hook_commands", None)
    monkeypatch.delattr(cli_pkg, "hook_commands", raising=False)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload(tool, project))))
    monkeypatch.setattr(sys, "stdout", out)
    code = cli_main(["hook", tool, "--enforce", *flags])
    if flags:
        assert denies(tool, code, out.getvalue()), (code, out.getvalue())
    else:  # not enforcing strictly: the hook cannot judge, and does not block
        assert code == 0 and not denies(tool, code, out.getvalue() or "{}")


# --- class B: one validator for every host and event kind ---------------------------------------------------------

def _events(tool, project, **loc):
    """Complete, judged events of every kind the host sends, as ``{name: payload}``; ``loc`` overrides where the
    event says it runs (cwd, workspace_roots)."""
    where = {"cwd": str(project), **loc}
    read = str(project / "notes.txt")
    if tool == "cursor":
        roots = {"workspace_roots": loc.get("workspace_roots", [str(project)])}
        base = {"conversation_id": "c", "generation_id": "g", "model": "m", "cursor_version": "1",
                "transcript_path": None, **roots, "cwd": where["cwd"]}

        def event(**fields):
            return dict(base, **fields)

        return {
            "shell": event(hook_event_name="beforeShellExecution", command="echo hi"),
            "mcp": event(hook_event_name="beforeMCPExecution", mcp_server_name="srv", tool_name="run"),
            "read": event(hook_event_name="beforeReadFile", file_path=read),
            "pre_write": event(hook_event_name="preToolUse", tool_name="Write", tool_input={"file_path": read}),
            "pre_read": event(hook_event_name="preToolUse", tool_name="Read", tool_input={"file_path": read}),
            "pre_shell": event(hook_event_name="preToolUse", tool_name="Shell", tool_input={"command": "echo hi"}),
            "pre_mcp": event(hook_event_name="preToolUse", tool_name="mcp__srv__run", tool_input={}),
            "pre_web": event(hook_event_name="preToolUse", tool_name="WebFetch",
                             tool_input={"url": "https://example.com/x"}),
        }
    base = {"session_id": "s", "transcript_path": None, "cwd": where["cwd"], "hook_event_name": "PreToolUse",
            "tool_use_id": "t"}

    def event(tool_name, tool_input):
        return dict(base, tool_name=tool_name, tool_input=tool_input)

    out = {
        "bash": event("Bash", {"command": "echo hi"}),
        "mcp": event("mcp__srv__run", {}),
        "write": event("Write", {"file_path": read}),
    }
    if tool == "codex":
        out["patch"] = event("apply_patch", {"command": PATCH})
    else:
        out["read"] = event("Read", {"file_path": read})
        out["web"] = event("WebFetch", {"url": "https://example.com/x"})
        out["search"] = event("WebSearch", {"query": "x"})
    return out


def _observe(home):
    (home / ".briefcase").mkdir(exist_ok=True)
    (home / ".briefcase" / "policy.toml").write_text('version = 1\npreset = "observe"\n')


MISSING = {"missing": {"cwd": None, "workspace_roots": []},
           "relative": {"cwd": "sub/dir", "workspace_roots": ["rel/root"]}}
B_COMBOS = [(t, k, m) for t in TOOLS for k in _events(t, __import__("pathlib").Path("/p")) for m in MISSING]


@pytest.mark.parametrize("tool, kind, missing", B_COMBOS, ids=[f"{t}-{k}-{m}" for t, k, m in B_COMBOS])
@pytest.mark.parametrize("entry", ["module", "launcher"])
def test_a_strict_hook_denies_every_judged_event_that_does_not_say_where_it_runs(
        home, project, monkeypatch, tool, kind, missing, entry):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CURSOR_PROJECT_DIR", raising=False)
    _observe(home)
    body = _events(tool, project, **MISSING[missing])[kind]
    code, raw = run_entry(entry, tool, ["--fail-closed"], body, home)
    assert denies(tool, code, raw), f"{tool} {kind} with {missing} location was allowed: {code} {raw!r}"


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("entry", ["module", "launcher"])
def test_the_complete_events_the_validator_checks_are_allowed(home, project, tool, entry):
    _observe(home)
    for kind, body in _events(tool, project).items():
        code, raw = run_entry(entry, tool, ["--fail-closed"], body, home)
        assert not denies(tool, code, raw), f"{tool} {kind} is complete but was denied: {code} {raw!r}"


BAD_URLS = ["relative/path", "example.com/x", "https://", "file:///etc/passwd", "//example.com/x", "http:///x",
            "https:// ", "https://@/x", "https://:443/x", "javascript:alert(1)"]


@pytest.mark.parametrize("url", BAD_URLS)
@pytest.mark.parametrize("tool", ["claude-code", "cursor"])
@pytest.mark.parametrize("entry", ["module", "launcher"])
def test_a_strict_hook_denies_a_url_without_a_scheme_and_an_authority(home, project, tool, url, entry):
    _observe(home)
    body = _events(tool, project)["pre_web" if tool == "cursor" else "web"]
    body["tool_input"] = {"url": url}
    code, raw = run_entry(entry, tool, ["--fail-closed"], body, home)
    assert denies(tool, code, raw), f"{url!r} was allowed: {code} {raw!r}"


def test_the_oracle_ignores_denial_json_on_exit_codes_the_hosts_do_not_read():
    deny = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"}})
    assert denies("codex", 0, deny)
    assert not denies("codex", 1, deny) and not denies("claude-code", 3, deny)


# --- round 14: the handlers pass flags only, in any order, and the CLI module's own imports are covered -----------

BINDING_FIRST = [
    ["--require-policy-sha256=" + "0" * 64, "--enforce"],
    ["--require-policy-sha256", "0" * 64, "--enforce"],
    ["--fail-closed", "--enforce"],
]


@pytest.mark.parametrize("order", BINDING_FIRST, ids=["hash_eq_first", "hash_sep_first", "flag_first"])
@pytest.mark.parametrize("failure", ["dispatcher_import", "hook_import"])
@pytest.mark.parametrize("tool", TOOLS)
def test_a_binding_option_that_comes_first_still_blocks_when_the_portable_command_fails(
        home, project, monkeypatch, tool, failure, order):
    import briefcase.cli as cli_pkg
    import briefcase.policy as policy_pkg

    build_state(home, project, "valid_denying")
    if failure == "dispatcher_import":
        monkeypatch.setitem(sys.modules, "briefcase.cli.hook_commands", None)
        monkeypatch.delattr(cli_pkg, "hook_commands", raising=False)
    else:
        monkeypatch.setitem(sys.modules, "briefcase.policy.hook", None)
        monkeypatch.delattr(policy_pkg, "hook", raising=False)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload(tool, project))))
    monkeypatch.setattr(sys, "stdout", out)
    code = cli_main(["hook", tool, *order])
    assert denies(tool, code, out.getvalue()), (code, out.getvalue())


@pytest.mark.parametrize("flags", [["--fail-closed"], ["--require-policy-sha256=" + "0" * 64]], ids=["flag", "hash"])
@pytest.mark.parametrize("broken", ["briefcase.cli.state", "briefcase.cli.engine", "briefcase.cli._ux"])
@pytest.mark.parametrize("tool", TOOLS)
def test_an_import_failure_in_the_cli_module_itself_still_blocks_a_binding_hook(
        home, project, monkeypatch, tool, broken, flags):
    import importlib

    build_state(home, project, "valid_denying")
    monkeypatch.delitem(sys.modules, "briefcase.cli.__main__", raising=False)
    monkeypatch.setitem(sys.modules, broken, None)
    entry = importlib.import_module("briefcase.cli.__main__")  # imports fail inside: the module must still load
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload(tool, project))))
    monkeypatch.setattr(sys, "stdout", out)
    code = entry.main(["hook", tool, *flags, "--enforce"])
    assert denies(tool, code, out.getvalue()), (code, out.getvalue())


# --- round 14: validate exactly what evaluation consumes ---------------------------------------------------------

MIXED_URLS = ["https://example.com:abc/x", "https://example.com:99999/x", "https://exa\\mple.com/x",
              "https://[::1/x", "https:// example.com/x"]


@pytest.mark.parametrize("url", MIXED_URLS)
@pytest.mark.parametrize("tool", ["claude-code", "cursor"])
def test_a_strict_hook_denies_an_authority_whose_parse_differs_from_the_one_rules_match(home, project, tool, url):
    _observe(home)
    body = _events(tool, project)["pre_web" if tool == "cursor" else "web"]
    body["tool_input"] = {"url": url}
    code, raw = run_entry("module", tool, ["--fail-closed"], body, home)
    assert denies(tool, code, raw), f"{url!r} was allowed: {code} {raw!r}"


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("kind", ["bash", "write", "mcp", "pre_shell", "read", "pre_write", "pre_mcp"])
def test_a_relative_project_root_beside_an_absolute_cwd_is_not_a_location(home, project, monkeypatch, tool, kind):
    """Policy is loaded from the project directory when there is one, so that is the path that must be absolute."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    events = _events(tool, project)
    if kind not in events:
        pytest.skip("not an event of this host")
    _observe(home)
    body = events[kind]
    if tool == "cursor":
        body["workspace_roots"] = ["relative/root"]
    else:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "relative/root")
        if tool == "codex":
            pytest.skip("Codex has no project directory variable")
    code, raw = run_entry("module", tool, ["--fail-closed"], body, home)
    assert denies(tool, code, raw), (tool, kind, code, raw)


def test_a_web_search_with_no_query_is_denied_by_a_strict_hook(home, project):
    _observe(home)
    body = _events("claude-code", project)["search"]
    body["tool_input"] = {}
    code, raw = run_entry("module", "claude-code", ["--fail-closed"], body, home)
    assert denies("claude-code", code, raw)


# --- round 15: a hook bound to a required policy allows only what that policy explicitly allows -------------------

def _bound(tool, project, policy_text, tmp_path, monkeypatch, call=None, entry="module"):
    import hashlib

    system = tmp_path / "system" / "policy.toml"
    system.parent.mkdir(exist_ok=True)
    system.write_text(policy_text)
    monkeypatch.setenv("BRIEFCASE_SYSTEM_POLICY", str(system))
    body = call if call is not None else payload(tool, project)
    flags = ["--require-policy-sha256", hashlib.sha256(policy_text.encode()).hexdigest()]
    return run_entry(entry, tool, flags, body, project.parent / "home")


def _harmless(tool, project):
    body = payload(tool, project)
    if tool == "cursor":
        body["command"] = "ls"
    else:
        body["tool_input"] = {"command": "ls"}
    return body


EXPLICIT = [
    # (policy text, the harmless call is allowed?)
    ('version = 1\npreset = "guarded"\n', False),                      # nothing says allow: implicit default only
    ('version = 1\npreset = "guarded"\ndefault = "allow"\n', True),     # the policy declares the default itself
    ('version = 1\npreset = "guarded"\n[[rule]]\nid = "ls"\ntool = "shell"\neffect = "allow"\ncommand = ["ls"]\n',
     True),                                                            # a rule allows it
    ('version = 1\npreset = "observe"\n', False),                      # observe logs; it does not authorize
    ('version = 1\npreset = "guarded"\nmode = "observe"\n[[rule]]\nid = "no-ls"\ntool = "shell"\neffect = "deny"\n'
     'command = ["ls"]\n', False),                                     # observe must not turn a matched deny into allow
]


@pytest.mark.parametrize("policy, allowed", EXPLICIT, ids=["implicit", "declared_default", "allow_rule", "observe",
                                                          "observe_deny"])
@pytest.mark.parametrize("entry", ["module", "launcher"])
@pytest.mark.parametrize("tool", TOOLS)
def test_a_bound_hook_allows_only_what_the_required_policy_explicitly_allows(
        home, project, tmp_path, monkeypatch, tool, policy, allowed, entry):
    code, raw = _bound(tool, project, policy, tmp_path, monkeypatch, _harmless(tool, project), entry)
    assert (not denies(tool, code, raw)) == allowed, (code, raw)
