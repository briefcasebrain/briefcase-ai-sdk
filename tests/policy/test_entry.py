"""The ``briefcase-ai`` console script decides whether argv is a hook invocation before it imports anything of this
SDK, so a hook gives its verdict however broken the rest of the package is. Run on a cold interpreter: a fresh
python process whose import system refuses the named modules."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.policy.test_hook_invariants import TOOLS, build_state, denies, payload

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX paths")

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _forget_stderr():
    from tests.policy import test_hook_invariants as invariants

    invariants.LAST_STDERR = None  # an in-process test never captured stderr


BLOCKED = ["briefcase.cli.engine", "briefcase.cli.state", "briefcase._logging", "briefcase.decorators",
           "briefcase.cli", "briefcase.policy.hook_commands"]
LEADING = [[], ["--debug"], ["--no-input", "--debug"], ["--server", "host:1", "--repo=r"], ["--deb"], ["--dry-run"]]

PRELUDE = """
import sys
BLOCKED = %r
class Block:
    def find_spec(self, name, path=None, target=None):
        if any(name == b or name.startswith(b + ".") for b in BLOCKED):
            raise ImportError("blocked for the test: " + name)
sys.meta_path.insert(0, Block())
sys.argv = ["briefcase-ai"] + %r
if %r:
    import os
    os.close(2)  # stderr cannot carry anything
import importlib
module_name, _, func = %r.partition(":")
sys.exit(getattr(importlib.import_module(module_name), func)())
"""


def console_target():
    text = (ROOT / "pyproject.toml").read_text()
    return re.search(r'^briefcase-ai = "([^"]+)"', text, re.M).group(1)


def run_console(argv, blocked, home, stdin, close_stderr=False):
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT),
           "BRIEFCASE_SYSTEM_POLICY": os.environ["BRIEFCASE_SYSTEM_POLICY"]}
    script = PRELUDE % (blocked, argv, close_stderr, console_target())
    done = subprocess.run([sys.executable, "-c", script], input=stdin, capture_output=True, text=True, env=env,
                          timeout=60)
    from tests.policy import test_hook_invariants as invariants

    invariants.LAST_STDERR = done.stderr  # the decoder needs it: Codex ignores exit 2 without a reason
    return done


@pytest.mark.parametrize("leading", LEADING, ids=lambda v: "-".join(v) or "none")
@pytest.mark.parametrize("blocked", BLOCKED)
@pytest.mark.parametrize("tool", TOOLS)
def test_a_hook_invocation_gives_its_verdict_whatever_else_fails_to_import(home, project, tool, blocked, leading):
    build_state(home, project, "valid_denying")
    done = run_console([*leading, "hook", tool, "--require-policy-sha256=" + "0" * 64, "--enforce"], [blocked], home,
                       json.dumps(payload(tool, project)))
    assert denies(tool, done.returncode, done.stdout), (done.returncode, done.stdout, done.stderr)


@pytest.mark.parametrize("tool", TOOLS)
def test_a_hook_that_binds_nothing_still_fails_open_on_a_broken_package(home, project, tool):
    done = run_console(["hook", tool, "--enforce"], ["briefcase.cli.engine", "briefcase.cli.state"], home,
                       json.dumps(payload(tool, project)))
    assert done.returncode == 0 and not denies(tool, done.returncode, done.stdout or "{}"), done


def test_the_console_script_still_runs_ordinary_commands(home):
    done = run_console(["--version"], [], home, "")
    assert done.returncode == 0 and "briefcase" in done.stdout.lower(), done


# --- round 16: any binding flag makes the hook strict on every path, with or without --enforce -------------------

BINDING = [["--fail-closed"], ["--require-policy-sha256=" + "0" * 64], ["--require-policy-sha256", "0" * 64]]
ENFORCE = [["--enforce"], []]


@pytest.mark.parametrize("enforce", ENFORCE, ids=["enforce", "no_enforce"])
@pytest.mark.parametrize("flags", BINDING, ids=["flag", "hash_eq", "hash_sep"])
@pytest.mark.parametrize("tool", TOOLS)
def test_a_binding_flag_makes_a_hook_strict_with_or_without_enforce(home, project, tool, flags, enforce):
    build_state(home, project, "valid_denying")
    done = run_console(["hook", tool, *enforce, *flags], ["briefcase.policy.hook"], home,
                       json.dumps(payload(tool, project)))
    assert denies(tool, done.returncode, done.stdout), (done.returncode, done.stdout, done.stderr)


@pytest.mark.parametrize("enforce", ENFORCE, ids=["enforce", "no_enforce"])
@pytest.mark.parametrize("tool", TOOLS)
def test_a_hook_that_binds_nothing_is_never_blocked_by_a_failure(home, project, tool, enforce):
    done = run_console(["hook", tool, *enforce], ["briefcase.policy.hook"], home, json.dumps(payload(tool, project)))
    assert done.returncode == 0 and not denies(tool, done.returncode, done.stdout or "{}"), done


@pytest.mark.parametrize("blocked", [["runpy"], ["briefcase.policy.hook"], []])
@pytest.mark.parametrize("flags", BINDING, ids=["flag", "hash_eq", "hash_sep"])
@pytest.mark.parametrize("tool", TOOLS)
def test_the_denial_reaches_stdout_when_stderr_cannot_carry_it(home, project, tool, flags, blocked):
    """The entry's outer fallback and the launcher share one delivery routine: the reason on stderr with exit 2, else
    the JSON denial on stdout with exit 0."""
    build_state(home, project, "valid_denying")
    done = run_console(["hook", tool, "--enforce", *flags], blocked, home, json.dumps(payload(tool, project)),
                       close_stderr=True)
    assert denies(tool, done.returncode, done.stdout), (done.returncode, done.stdout, done.stderr)


def test_the_entry_package_is_declared_for_the_wheel_and_the_console_script():
    """Without the maturin declaration the wheel would name a module it does not contain."""
    text = (ROOT / "pyproject.toml").read_text()
    assert console_target() == "briefcase_entry:main"
    assert re.search(r'^python-packages = \[[^\]]*"briefcase_entry"', text, re.M)
    assert (ROOT / "briefcase_entry" / "__init__.py").is_file()


# --- round 17: every dispatch site consults optargs.enforcing; the entry carries its own fixed denial ---------------

def test_every_function_that_routes_a_hook_invocation_consults_enforcing():
    """Grep-driven: any function that calls run_cursor/run_enforce, or compares an argument list with "--enforce",
    must itself consult enforcing() (the one rule); a new dispatch site cannot skip it. `dispatch` is the one function
    that may call the two runners."""
    import ast

    sites, callers = {}, {}
    for path in list((ROOT / "briefcase").rglob("*.py")) + [ROOT / "briefcase_entry" / "__init__.py"]:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            called = {c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", "")
                      for c in ast.walk(node) if isinstance(c, ast.Call)}
            names = {n.attr if isinstance(n, ast.Attribute) else n.id for n in ast.walk(node)
                     if isinstance(n, (ast.Attribute, ast.Name))}
            compares = any(isinstance(c, ast.Constant) and c.value == "--enforce" for c in ast.walk(node)
                           if isinstance(c, ast.Constant)) and any(isinstance(n, ast.Compare) for n in ast.walk(node))
            routes = names & {"run_cursor", "run_enforce"}
            if routes or compares:
                consults = bool(called & {"enforcing", "enforcing_for", "_enforcing"})
                sites[f"{path.relative_to(ROOT)}::{node.name}"] = consults
            if routes:
                callers[f"{path.relative_to(ROOT)}::{node.name}"] = routes
    assert sites, "the scan found no dispatch site at all: the test is not looking at the right places"
    # These write or recognise the text of a hook command (and `connect --enforce`); they route nothing.
    exempt = {"briefcase/policy/optargs.py::enforcing",  # it is the rule
              "briefcase/policy/presets.py::self_command", "briefcase/policy/install.py::hook_command",
              "briefcase/policy/install.py::_command_in"}
    missing = [name for name, consults in sites.items() if not consults and name not in exempt]
    assert not missing, f"these routing functions do not consult enforcing(): {missing}"
    # run_cursor (the forwarder) and run_enforce may only be called from dispatch, which consults enforcing.
    called_outside = [name for name in callers if not name.endswith("::dispatch")]
    assert not called_outside, called_outside


@pytest.mark.parametrize("flags", [["--require-policy-sha256=" + "0" * 64], ["--fail-closed"]], ids=["hash", "flag"])
def test_in_process_cursor_without_enforce_and_a_binding_flag_is_enforced(home, project, monkeypatch, flags):
    import io

    from briefcase.cli.__main__ import main as cli_main

    build_state(home, project, "valid_denying")
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload("cursor", project))))
    monkeypatch.setattr(sys, "stdout", out)
    code = cli_main(["hook", "cursor", *flags])  # no --enforce
    assert denies("cursor", code, out.getvalue()), (code, out.getvalue())


@pytest.mark.parametrize("flags", [["--require-policy-sha256=" + "0" * 64], ["--fail-closed"]], ids=["hash", "flag"])
def test_in_process_cursor_import_failure_without_enforce_and_a_binding_flag_still_blocks(
        home, project, monkeypatch, flags):
    import io

    import briefcase.cli as cli_pkg
    from briefcase.cli.__main__ import main as cli_main

    build_state(home, project, "valid_denying")
    monkeypatch.setitem(sys.modules, "briefcase.cli.hook_commands", None)
    monkeypatch.delattr(cli_pkg, "hook_commands", raising=False)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload("cursor", project))))
    monkeypatch.setattr(sys, "stdout", out)
    code = cli_main(["hook", "cursor", *flags])
    assert denies("cursor", code, out.getvalue()), (code, out.getvalue())


def run_with_stub_briefcase(tmp_path, argv, home, stdin, close_stderr=False):
    """The entry on a cold interpreter where `briefcase` is a bare package with no policy/hook_main.py."""
    stub = tmp_path / "stub"
    (stub / "briefcase").mkdir(parents=True, exist_ok=True)
    (stub / "briefcase" / "__init__.py").write_text("")
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", ""), "PYTHONPATH": f"{stub}{os.pathsep}{ROOT}"}
    script = PRELUDE % ([], argv, close_stderr, "briefcase_entry:main")
    done = subprocess.run([sys.executable, "-c", script], input=stdin, capture_output=True, text=True, env=env,
                          timeout=60)
    from tests.policy import test_hook_invariants as invariants

    invariants.LAST_STDERR = done.stderr  # a bare Codex exit 2 (no reason) must not pass for a block
    return done


@pytest.mark.parametrize("flags", BINDING, ids=["flag", "hash_eq", "hash_sep"])
@pytest.mark.parametrize("tool", TOOLS)
def test_a_missing_launcher_still_delivers_the_denial_text(home, project, tmp_path, tool, flags):
    done = run_with_stub_briefcase(tmp_path, ["hook", tool, "--enforce", *flags], home,
                                   json.dumps(payload(tool, project)))
    assert done.returncode == 2 and "Blocked by briefcase-ai policy" in done.stderr, done
    closed = run_with_stub_briefcase(tmp_path, ["hook", tool, "--enforce", *flags], home,
                                     json.dumps(payload(tool, project)), close_stderr=True)
    assert denies(tool, closed.returncode, closed.stdout), closed


def test_the_entrys_fixed_denial_is_byte_for_byte_the_launchers():
    import importlib.util

    import briefcase_entry

    path = ROOT / "briefcase" / "policy" / "hook_main.py"
    spec = importlib.util.spec_from_file_location("_hook_main_for_test", path)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    assert (briefcase_entry._REASON, briefcase_entry._DENY, briefcase_entry._DENY_CURSOR) == (
        launcher._REASON, launcher._DENY, launcher._DENY_CURSOR)


def test_the_dispatch_site_scan_counts_calls_and_not_docstrings(tmp_path):
    """A docstring that says "enforcing" is not a call: the scan must not accept it."""
    import ast

    source = 'def route(argv):\n    """consults enforcing() and enforcing_for"""\n    return run_cursor(argv)\n'
    tree = ast.parse(source)
    (func,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    called = {c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", "")
              for c in ast.walk(func) if isinstance(c, ast.Call)}
    assert not called & {"enforcing", "enforcing_for", "_enforcing"}


@pytest.mark.parametrize("leading", [["--debug"], ["--server", "h:1"], ["--no-input", "--deb"]])
@pytest.mark.parametrize("failure", ["stored_import_error", "dispatcher_import"])
@pytest.mark.parametrize("tool", TOOLS)
def test_the_in_process_cli_recognizes_a_hook_after_global_options_with_the_consoles_scanner(
        home, project, monkeypatch, tool, failure, leading):
    import io

    import briefcase.cli as cli_pkg
    import briefcase.cli.__main__ as entry_module

    build_state(home, project, "valid_denying")
    if failure == "stored_import_error":
        monkeypatch.setattr(entry_module, "_IMPORT_ERROR", ImportError("a command module cannot be imported"))
    else:
        monkeypatch.setitem(sys.modules, "briefcase.cli.hook_commands", None)
        monkeypatch.delattr(cli_pkg, "hook_commands", raising=False)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload(tool, project))))
    monkeypatch.setattr(sys, "stdout", out)
    code = entry_module.main([*leading, "hook", tool, "--require-policy-sha256=" + "0" * 64, "--enforce"])
    assert denies(tool, code, out.getvalue()), (code, out.getvalue())


LAUNCHER_WITHOUT_AN_ENTRY = """
import os, sys
def enforcing_for(tool, options): return True
def strict_for(options): return True
def load_sibling(name): raise ImportError(name)
# a launcher that loads and never enters main
"""


@pytest.mark.parametrize("flags", BINDING, ids=["flag", "hash_eq", "hash_sep"])
@pytest.mark.parametrize("tool", TOOLS)
def test_a_launcher_that_loads_but_never_enters_main_is_a_denial(home, project, tmp_path, tool, flags):
    stub = tmp_path / "stub"
    (stub / "briefcase" / "policy").mkdir(parents=True)
    (stub / "briefcase" / "__init__.py").write_text("")
    (stub / "briefcase" / "policy" / "hook_main.py").write_text(LAUNCHER_WITHOUT_AN_ENTRY)
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", ""), "PYTHONPATH": f"{stub}{os.pathsep}{ROOT}"}
    script = PRELUDE % ([], ["hook", tool, "--enforce", *flags], False, "briefcase_entry:main")
    done = subprocess.run([sys.executable, "-c", script], input=json.dumps(payload(tool, project)),
                          capture_output=True, text=True, env=env, timeout=60)
    from tests.policy import test_hook_invariants as invariants

    invariants.LAST_STDERR = done.stderr
    assert denies(tool, done.returncode, done.stdout), (done.returncode, done.stdout, done.stderr)


@pytest.mark.parametrize("flags", BINDING, ids=["flag", "hash_eq", "hash_sep"])
@pytest.mark.parametrize("leading", [[], ["--debug"], ["--server", "h:1"], ["--no-input", "--deb"]])
@pytest.mark.parametrize("dispatcher", ["loads", "broken", "nothing_loads"])
@pytest.mark.parametrize("tool", TOOLS)
def test_a_binding_flag_anywhere_blocks_when_the_scanner_and_the_cli_imports_fail(
        home, project, monkeypatch, tool, dispatcher, leading, flags):
    import io

    import briefcase.cli as cli_pkg
    import briefcase.cli.__main__ as entry_module

    build_state(home, project, "valid_denying")
    monkeypatch.setitem(sys.modules, "briefcase_entry", None)  # the shared scanner cannot be imported
    monkeypatch.setattr(entry_module, "_IMPORT_ERROR", ImportError("a command module cannot be imported"))
    if dispatcher != "loads":
        monkeypatch.setitem(sys.modules, "briefcase.cli.hook_commands", None)
        monkeypatch.delattr(cli_pkg, "hook_commands", raising=False)
    if dispatcher == "nothing_loads":
        monkeypatch.setitem(sys.modules, "briefcase.policy.hook_main", None)
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload(tool, project))))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = entry_module.main([*leading, "hook", tool, *flags, "--enforce"])
    assert code == 2 and err.getvalue().strip() or denies(tool, code, out.getvalue()), (code, out.getvalue())


# --- the locally generated strict command enters the launcher through its named main ----------------------------

def run_generated(tool, fail_closed, launcher_text, tmp_path, monkeypatch, home, project):
    import shlex

    from briefcase.policy import install

    stub = tmp_path / "hook_main.py"
    if launcher_text is not None:
        stub.write_text(launcher_text)
    monkeypatch.setattr(install, "launcher_path", lambda: str(stub))
    command = install.hook_command(tool, install.HookOptions(fail_closed=fail_closed, endpoint="http://127.0.0.1:1"))
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
    done = subprocess.run(shlex.split(command), input=json.dumps(payload(tool, project)), capture_output=True,
                          text=True, env=env, timeout=60)
    from tests.policy import test_hook_invariants as invariants

    invariants.LAST_STDERR = done.stderr
    return done


@pytest.mark.parametrize("launcher", ["# truncated: valid Python that never defines or enters main\nx = 1\n",
                                      "def main():\n    return 'not a status'\n", None],
                         ids=["no_main", "bad_main", "missing"])
@pytest.mark.parametrize("tool", TOOLS)
def test_the_strict_local_command_blocks_when_the_launcher_has_no_working_main(
        home, project, tmp_path, monkeypatch, tool, launcher):
    done = run_generated(tool, True, launcher, tmp_path, monkeypatch, home, project)
    assert done.returncode == 2 and done.stderr.strip(), (done.returncode, done.stdout, done.stderr)


@pytest.mark.parametrize("tool", TOOLS)
def test_the_strict_local_command_still_runs_the_real_launcher(home, project, monkeypatch, tool, tmp_path):
    import shlex

    from briefcase.policy import install

    build_state(home, project, "valid_denying")
    command = install.hook_command(tool, install.HookOptions(fail_closed=True, endpoint="http://127.0.0.1:1"))
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", ""),
           "BRIEFCASE_SYSTEM_POLICY": os.environ["BRIEFCASE_SYSTEM_POLICY"]}
    done = subprocess.run(shlex.split(command), input=json.dumps(payload(tool, project)), capture_output=True,
                          text=True, env=env, timeout=60)
    assert denies(tool, done.returncode, done.stdout), (done.returncode, done.stdout, done.stderr)


def _script():
    import importlib.util

    path = ROOT / "scripts" / "check_installed_entry.py"
    spec = importlib.util.spec_from_file_location("check_installed_entry", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_smoke_check_accepts_only_a_complete_host_verdict():
    script = _script()

    class Done:
        def __init__(self, code, out="", err=""):
            self.returncode, self.stdout, self.stderr = code, out, err

    full = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"}})
    nameless = json.dumps({"hookSpecificOutput": {"permissionDecision": "deny"}})
    assert script.blocked("claude-code", Done(0, full))
    assert not script.blocked("claude-code", Done(0, nameless)), "the host ignores a denial without the event name"
    assert not script.blocked("codex", Done(2, "", "")), "Codex ignores exit 2 with no reason"
    assert script.blocked("codex", Done(2, "", "reason"))
    assert script.blocked("cursor", Done(0, json.dumps({"permission": "deny"})))


def test_the_smoke_events_are_complete_enough_to_be_judged_on_the_binding(tmp_path):
    script = _script()
    assert script.event("claude-code", str(tmp_path))["cwd"] == str(tmp_path)
    assert script.event("cursor", str(tmp_path))["cwd"] == str(tmp_path)


def test_the_wheel_content_check_names_a_wheel_that_omits_the_entry_package(tmp_path):
    import zipfile

    script = _script()
    good = tmp_path / "good-1-py3-none-any.whl"
    bad = tmp_path / "bad-1-py3-none-any.whl"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("briefcase_entry/__init__.py", "")
        z.writestr("x.dist-info/entry_points.txt", "[console_scripts]\nbriefcase-ai=briefcase_entry:main\n")
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("briefcase/__init__.py", "")
        z.writestr("x.dist-info/entry_points.txt", "[console_scripts]\nbriefcase-ai=briefcase.cli.__main__:main\n")
    problems = script.wheel_contents(str(tmp_path))
    assert any("bad-1" in p and "briefcase_entry/__init__.py" in p for p in problems)
    assert not any("good-1" in p for p in problems)


@pytest.mark.parametrize("argv_shape", [
    ["--require-policy-sha256=" + "0" * 64, "hook", "codex", "--enforce"],        # a binding before `hook`
    ["--fail-closed", "--debug", "hook", "cursor"],                                 # before, and no --enforce
    ["--server", "hook", "hook", "codex", "--fail-closed"],                         # a global value named `hook`
    ["--repo", "hook", "--fail-closed", "hook", "claude-code", "--enforce"],
], ids=["binding_before_hook", "binding_and_global_before_cursor", "value_named_hook", "value_named_hook_2"])
def test_a_scanner_failure_blocks_directly_whatever_the_position_of_the_binding(
        home, project, monkeypatch, argv_shape):
    import io

    import briefcase.cli.__main__ as entry_module

    monkeypatch.setitem(sys.modules, "briefcase_entry", None)  # the shared scanner cannot be imported
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = entry_module.main(argv_shape)
    assert code == 2 and err.getvalue().strip() and not out.getvalue().strip(), (code, out.getvalue(), err.getvalue())
    monkeypatch.setattr(sys, "stderr", None)  # stderr unusable: the JSON denial goes to stdout, in the format of the
    out = io.StringIO()                       # host the command line names (Cursor's differs), never the wrong one
    monkeypatch.setattr(sys, "stdout", out)
    code = entry_module.main(argv_shape)
    named = "cursor" if "cursor" in argv_shape else "claude-code"
    assert code == 0 and denies(named, 0, out.getvalue()), (code, out.getvalue())


def _run_generated_command(tool, launcher_dir_name, launcher_text, tmp_path, monkeypatch, home, project,
                           close_stderr=False):
    import shlex

    from briefcase.policy import install

    directory = tmp_path / launcher_dir_name
    directory.mkdir()
    stub = directory / "hook_main.py"
    stub.write_text(launcher_text)
    monkeypatch.setattr(install, "launcher_path", lambda: str(stub))
    command = install.hook_command(tool, install.HookOptions(fail_closed=True, endpoint="http://127.0.0.1:1"))
    child = subprocess.Popen(shlex.split(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env={"HOME": str(home), "PATH": os.environ.get("PATH", "")})
    if close_stderr:
        child.stderr.close()
    child.stdin.write(json.dumps(payload(tool, project)).encode())
    child.stdin.close()
    out = child.stdout.read().decode()
    err = "" if close_stderr else child.stderr.read().decode()
    child.wait(timeout=60)
    return child.returncode, out, err


@pytest.mark.parametrize("tool", TOOLS)
def test_the_strict_local_command_turns_any_exit_during_loading_into_a_denial(
        home, project, tmp_path, monkeypatch, tool):
    code, out, err = _run_generated_command(tool, "d", "import sys\nsys.exit(0)  # a silent success while loading\n",
                                            tmp_path, monkeypatch, home, project)
    assert code == 2 and err.strip(), (code, out, err)


@pytest.mark.parametrize("tool", TOOLS)
def test_the_strict_local_command_delivers_the_json_denial_when_stderr_is_unusable(
        home, project, tmp_path, monkeypatch, tool):
    code, out, err = _run_generated_command(tool, "d", "x = 1\n", tmp_path, monkeypatch, home, project,
                                            close_stderr=True)
    assert code == 0 and denies(tool, 0, out), (code, out)


@pytest.mark.parametrize("tool", TOOLS)
def test_the_strict_local_command_keeps_the_launchers_own_status_and_a_path_with_non_bmp_characters(
        home, project, tmp_path, monkeypatch, tool):
    """The launcher's verdict is the status main() returns; and a virtualenv path with an emoji reaches Python as an
    argument, not as source text where it would turn into surrogates."""
    code, out, err = _run_generated_command(tool, "venv-\U0001F642", "def main():\n    return 0\n",
                                            tmp_path, monkeypatch, home, project)
    assert (code, out) == (0, ""), (code, out, err)


# --- round 21: bindings before `hook` survive the scanner, and an unknown host gets a denial every host reads --------

@pytest.mark.parametrize("before", [["--require-policy-sha256=" + "0" * 64], ["--fail-closed"],
                                    ["--debug", "--fail-closed"]])
@pytest.mark.parametrize("tool", TOOLS)
def test_the_healthy_scanner_keeps_a_binding_that_stands_before_hook(home, project, tool, before):
    build_state(home, project, "valid_denying")
    done = run_console([*before, "hook", tool, "--enforce"], [], home, json.dumps(payload(tool, project)))
    assert denies(tool, done.returncode, done.stdout), (done.returncode, done.stdout, done.stderr)
    import briefcase_entry

    assert briefcase_entry.hook_invocation([*before, "hook", tool, "--enforce"]) == (tool, [*before, "--enforce"])


@pytest.mark.parametrize("host", TOOLS)
@pytest.mark.parametrize("imports", ["scanner_only", "scanner_and_policy"])
def test_an_unknown_host_gets_a_stdout_denial_every_host_reads_even_when_nothing_loads(
        home, project, monkeypatch, host, imports):
    import io

    import briefcase.cli.__main__ as entry_module

    monkeypatch.setitem(sys.modules, "briefcase_entry", None)
    if imports == "scanner_and_policy":
        monkeypatch.setitem(sys.modules, "briefcase.policy.hook_main", None)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", None)  # unusable: the denial must reach stdout
    # an unrelated option value that names another host must not decide the format
    code = entry_module.main(["--repo", "cursor", "--fail-closed", "hook", host, "--enforce"])
    assert code == 0 and denies(host, 0, out.getvalue()), (code, out.getvalue())
