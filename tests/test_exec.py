"""``briefcase exec`` and the interpreter-start bootstrap.

The end-to-end tests run real child interpreters: a script that knows
nothing about exporters is started through
``python -m briefcase.cli.exec_commands`` and its records are read back from
a ``.jsonl`` destination."""

import argparse
import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from briefcase._bootstrap import BOOTSTRAP_DIR
from briefcase.cli import exec_commands

REPO = Path(__file__).resolve().parent.parent

# A program with no briefcase setup of its own: it never calls observe().
APP = """
    import sys
    import briefcase

    @briefcase.capture
    def classify(text):
        return text.upper()

    classify("hello")
    print("app-ran")
    sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
"""


def _child_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("BRIEFCASE_", "OTEL_")) and k != "PYTHONPATH"}
    # The child must be able to import briefcase from this checkout.
    env["PYTHONPATH"] = str(REPO)
    env.update(extra)
    return env


def _exec(tmp_path, *args, env=None, script=APP, script_args=()):
    app = tmp_path / "app.py"
    app.write_text(textwrap.dedent(script))
    command = [sys.executable, "-m", "briefcase.cli.exec_commands", *args,
               "--", sys.executable, str(app), *script_args]
    return subprocess.run(
        command, cwd=str(tmp_path), env=env or _child_env(),
        capture_output=True, text=True, timeout=120,
    )


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# -- environment and argument handling --------------------------------------

def test_build_env_prepends_the_bootstrap_directory():
    env = exec_commands.build_env({"PYTHONPATH": os.pathsep.join(["/a", "/b"]), "KEEP": "1"})
    assert env["BRIEFCASE_AUTO"] == "1"
    assert env["PYTHONPATH"].split(os.pathsep) == [BOOTSTRAP_DIR, "/a", "/b"]
    assert env["KEEP"] == "1"
    assert "BRIEFCASE_EXPORTER" not in env


def test_build_env_is_idempotent_and_sets_options():
    once = exec_commands.build_env({}, to="runs.jsonl", capture_content="hash", skip="bedrock")
    twice = exec_commands.build_env(once)
    assert twice["PYTHONPATH"] == BOOTSTRAP_DIR
    assert twice["BRIEFCASE_EXPORTER"] == "runs.jsonl"
    assert twice["BRIEFCASE_CAPTURE_CONTENT"] == "hash"
    assert twice["BRIEFCASE_AUTO_SKIP"] == "bedrock"


def test_bootstrap_directory_holds_sitecustomize_and_no_pth_file():
    names = set(os.listdir(BOOTSTRAP_DIR))
    assert "sitecustomize.py" in names
    assert not any(name.endswith(".pth") for name in names)
    package = Path(BOOTSTRAP_DIR).parent
    assert not list(package.rglob("*.pth"))


def test_register_adds_an_exec_subcommand_like_the_other_modules():
    parser = argparse.ArgumentParser(prog="briefcase")
    sub = parser.add_subparsers(dest="command")
    exec_commands.register(sub)

    args = parser.parse_args(
        ["exec", "--to", "runs.jsonl", "--", "python", "app.py", "-v", "--flag"])
    assert args.func is exec_commands.cmd_exec
    assert args.to == "runs.jsonl"
    assert exec_commands._strip_separator(args.exec_command) == ["python", "app.py", "-v", "--flag"]


def test_handler_matches_the_cli_calling_convention(mocker):
    run = mocker.patch.object(exec_commands, "run", return_value=3)
    args = argparse.Namespace(exec_command=["--", "prog"], to=None, capture_content="full",
                              skip=None, no_replace=True)
    assert exec_commands.cmd_exec(args, object(), object(), object()) == 3
    run.assert_called_once_with(["--", "prog"], to=None, capture_content="full", skip=None,
                                replace_process=False)


def test_no_command_is_a_usage_error(capsys):
    assert exec_commands.run([]) == 2
    assert exec_commands.run(["--"]) == 2
    assert "no command given" in capsys.readouterr().err


def test_command_not_found_returns_127(capsys):
    assert exec_commands.run(["briefcase-no-such-program-xyz"], replace_process=False) == 127
    assert exec_commands.run(["briefcase-no-such-program-xyz"], replace_process=True) == 127
    assert "command not found" in capsys.readouterr().err


def test_child_mode_returns_the_exit_code():
    code = exec_commands.run(
        [sys.executable, "-c", "import sys; sys.exit(9)"],
        env=_child_env(), replace_process=False)
    assert code == 9


# -- end to end -------------------------------------------------------------

def test_exec_records_decisions_without_editing_the_program(tmp_path):
    out = tmp_path / "decisions.jsonl"
    result = _exec(tmp_path, "--to", str(out))

    assert result.returncode == 0, result.stderr
    assert "app-ran" in result.stdout
    records = _records(out)
    # async export on the background queue, drained by the atexit hook
    assert [r["function_name"] for r in records] == ["classify"]
    assert records[0]["schema_version"]


def test_exit_code_is_propagated_and_records_still_flush(tmp_path):
    out = tmp_path / "decisions.jsonl"
    result = _exec(tmp_path, "--to", str(out), script_args=("7",))
    assert result.returncode == 7
    assert len(_records(out)) == 1


def test_child_process_mode_propagates_the_exit_code(tmp_path):
    out = tmp_path / "decisions.jsonl"
    result = _exec(tmp_path, "--no-replace", "--to", str(out), script_args=("5",))
    assert result.returncode == 5
    assert len(_records(out)) == 1


def test_destination_comes_from_the_environment(tmp_path):
    out = tmp_path / "from-env.jsonl"
    result = _exec(tmp_path, env=_child_env(BRIEFCASE_EXPORTER=str(out)))
    assert result.returncode == 0, result.stderr
    assert len(_records(out)) == 1


def test_default_destination_is_stderr_not_stdout(tmp_path):
    result = _exec(tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == "app-ran"
    record = json.loads(result.stderr.strip().splitlines()[-1])
    assert record["function_name"] == "classify"


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_signals_are_propagated(tmp_path):
    script = """
        import os, signal
        os.kill(os.getpid(), signal.SIGTERM)
    """
    replaced = _exec(tmp_path, script=script)
    assert replaced.returncode == -signal.SIGTERM
    child = _exec(tmp_path, "--no-replace", script=script)
    assert child.returncode == 128 + signal.SIGTERM


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_signal_sent_to_the_wrapper_reaches_the_program(tmp_path):
    app = tmp_path / "wait.py"
    app.write_text(textwrap.dedent("""
        import signal, sys, time
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(42))
        print("ready", flush=True)
        time.sleep(60)
    """))
    for extra in ([], ["--no-replace"]):
        proc = subprocess.Popen(
            [sys.executable, "-m", "briefcase.cli.exec_commands", *extra,
             "--", sys.executable, str(app)],
            cwd=str(tmp_path), env=_child_env(BRIEFCASE_EXPORTER=str(tmp_path / "s.jsonl")),
            stdout=subprocess.PIPE, text=True,
        )
        try:
            assert proc.stdout.readline().strip() == "ready"
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=60) == 42
        finally:
            proc.kill()
            proc.stdout.close()


# -- the bootstrap itself ---------------------------------------------------

def _run_with_bootstrap(tmp_path, code, **env):
    env = _child_env(**env)
    env["PYTHONPATH"] = os.pathsep.join([BOOTSTRAP_DIR, env["PYTHONPATH"]])
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=str(tmp_path),
                          env=env, capture_output=True, text=True, timeout=120)


def test_bootstrap_does_nothing_without_briefcase_auto(tmp_path):
    result = _run_with_bootstrap(tmp_path, """
        import sys
        print("briefcase" in sys.modules, "sitecustomize" in sys.modules)
    """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["False", "True"]
    assert result.stderr == ""


def test_bootstrap_configures_the_exporter_and_defers_instrumentation(tmp_path):
    out = tmp_path / "o.jsonl"
    result = _run_with_bootstrap(tmp_path, """
        import sys
        import briefcase
        from briefcase.config import BriefcaseConfig
        heavy = [m for m in ("openai", "anthropic", "google.genai", "botocore", "langchain_core")
                 if m in sys.modules]
        print(type(BriefcaseConfig.get().exporter).__name__, heavy)
    """, BRIEFCASE_AUTO="1", BRIEFCASE_EXPORTER=str(out))
    assert result.returncode == 0, result.stderr
    # No SDK is imported at startup: each is patched when the program imports it.
    assert result.stdout.strip() == "JSONLFileExporter []"


def test_bootstrap_failure_never_reaches_the_program(tmp_path):
    result = _run_with_bootstrap(tmp_path, "print('still-running')",
                                 BRIEFCASE_AUTO="1", BRIEFCASE_EXPORTER="carrier-pigeon")
    assert result.returncode == 0
    assert result.stdout.strip() == "still-running"
    assert "briefcase: auto-instrumentation is disabled" in result.stderr
    assert "Traceback" not in result.stderr


def test_existing_sitecustomize_is_chained(tmp_path):
    other = tmp_path / "site_dir"
    other.mkdir()
    (other / "sitecustomize.py").write_text(
        "import builtins\nbuiltins.CHAINED_SITECUSTOMIZE = 'yes'\n")
    out = tmp_path / "o.jsonl"
    env = _child_env(BRIEFCASE_AUTO="1", BRIEFCASE_EXPORTER=str(out))
    env["PYTHONPATH"] = os.pathsep.join([BOOTSTRAP_DIR, str(other), env["PYTHONPATH"]])
    result = subprocess.run(
        [sys.executable, "-c",
         "import builtins, briefcase.config as c;"
         "print(builtins.CHAINED_SITECUSTOMIZE, type(c.BriefcaseConfig.get().exporter).__name__)"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["yes", "JSONLFileExporter"]


def test_broken_chained_sitecustomize_is_reported_not_raised(tmp_path):
    other = tmp_path / "site_dir"
    other.mkdir()
    (other / "sitecustomize.py").write_text("raise RuntimeError('broken user sitecustomize')\n")
    env = _child_env()
    env["PYTHONPATH"] = os.pathsep.join([BOOTSTRAP_DIR, str(other), env["PYTHONPATH"]])
    result = subprocess.run([sys.executable, "-c", "print('ok')"], cwd=str(tmp_path), env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0
    assert result.stdout.strip() == "ok"
    assert "error in the chained sitecustomize" in result.stderr


def test_real_sdk_is_instrumented_on_import_without_content(tmp_path):
    """Zero-code path with a real provider SDK: the program only imports and
    calls openai; the bootstrap patches it when it is imported."""
    pytest.importorskip("openai")
    out = tmp_path / "decisions.jsonl"
    script = """
        import sys
        assert "openai" not in sys.modules
        import openai
        httpx = sys.modules.get("httpx2") or sys.modules["httpx"]
        body = {"id": "c1", "object": "chat.completion", "created": 1, "model": "m-served",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "COMPLETION-SECRET"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}
        client = openai.OpenAI(api_key="k", http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))))
        client.chat.completions.create(
            model="m", messages=[{"role": "user", "content": "PROMPT-SECRET"}])
    """
    result = _exec(tmp_path, "--to", str(out), script=script)
    assert result.returncode == 0, result.stderr
    text = out.read_text()
    records = _records(out)
    assert [r["decision_type"] for r in records] == ["llm.openai.chat"]
    assert records[0]["token_usage"]["total_tokens"] == 5
    assert "PROMPT-SECRET" not in text and "COMPLETION-SECRET" not in text

    out.unlink()
    result = _exec(tmp_path, "--to", str(out), "--capture-content", "full", script=script)
    assert result.returncode == 0, result.stderr
    assert "PROMPT-SECRET" in out.read_text() and "COMPLETION-SECRET" in out.read_text()
