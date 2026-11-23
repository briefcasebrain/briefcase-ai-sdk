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
                                dedupe=None, replace_process=False, allow_uninstrumented=False)


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


# -- red team: preflight ------------------------------------------------------

@pytest.mark.parametrize("argv,blocked", [
    (["python", "app.py"], False),
    (["python3.12", "-u", "-X", "dev", "-W", "error", "app.py", "-I"], False),  # -I is the app's
    (["python", "-c", "print(1)", "-E"], False),
    (["python", "-m", "http.server", "-S"], False),
    (["python", "-Xdev", "-Werror", "-B", "app.py"], False),
    (["python", "-I", "app.py"], True),
    (["python", "-E", "-m", "app"], True),
    (["python", "-S", "app.py"], True),
    (["python", "-sIu", "app.py"], True),
    (["python", "-uE", "-c", "pass"], True),
    (["python", "-X", "dev", "-S", "app.py"], True),
    (["pypy3", "-I", "app.py"], True),
])
def test_interpreter_flags_that_defeat_the_bootstrap_are_detected(argv, blocked):
    assert bool(exec_commands.blocking_python_flags(argv)) is blocked


def test_a_command_that_cannot_be_instrumented_is_refused_with_a_reason(tmp_path, capsys):
    for flag, word in (("-I", "isolated"), ("-E", "PYTHONPATH"), ("-S", "sitecustomize")):
        code = exec_commands.run([sys.executable, flag, "-c", "print(1)"], env=_child_env(),
                                 replace_process=False)
        err = capsys.readouterr().err
        assert code == exec_commands.EXIT_USAGE
        assert "cannot be instrumented" in err and word in err and "--allow-uninstrumented" in err


def test_allow_uninstrumented_runs_it_with_a_warning(tmp_path):
    out = tmp_path / "o.jsonl"
    app = tmp_path / "app.py"
    app.write_text("print('ran-anyway')\n")
    done = subprocess.run(
        [sys.executable, "-m", "briefcase.cli.exec_commands", "--to", str(out),
         "--allow-uninstrumented", "--", sys.executable, "-I", str(app)],
        cwd=str(tmp_path), env=_child_env(), capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and "ran-anyway" in done.stdout
    assert "WITHOUT records" in done.stderr and not out.exists()


def test_a_script_whose_shebang_isolates_python_is_detected(tmp_path, capsys):
    script = tmp_path / "tool"
    script.write_text(f"#!{sys.executable} -sE\nprint('x')\n")
    script.chmod(0o755)
    assert exec_commands.uninstrumentable([str(script)])
    env_script = tmp_path / "tool2"
    env_script.write_text("#!/usr/bin/env -S python3 -I\nprint('x')\n")
    env_script.chmod(0o755)
    assert exec_commands.uninstrumentable([str(env_script)])
    fine = tmp_path / "tool3"
    fine.write_text(f"#!{sys.executable}\nprint('x')\n")
    fine.chmod(0o755)
    assert exec_commands.uninstrumentable([str(fine)]) == []
    assert exec_commands.uninstrumentable(["/bin/sh", "-c", "python -I x.py"]) == []
    assert exec_commands.uninstrumentable(["no-such-command-xyz"]) == []


@pytest.mark.parametrize("args,env,message", [
    (["--to", "consol"], {}, "is not a destination"),
    ([], {"BRIEFCASE_EXPORTER": "Console"}, "did you mean 'console'"),
    ([], {"OTEL_EXPORTER_OTLP_ENDPOINT": "collector:4317"}, "not a valid http"),
    (["--to", "otlp"], {"OTEL_EXPORTER_OTLP_PROTOCOL": "http/json"}, "not supported"),
    (["--skip", "langchian,openai"], {}, "unknown integration name(s)"),
    ([], {"BRIEFCASE_CAPTURE_CONTENT": "all"}, "is not one of none, hash, full"),
    ([], {"BRIEFCASE_AUTO_DEDUPE": "both"}, "is not one of none, provider, framework"),
])
def test_a_misconfiguration_stops_exec_before_the_program_runs(tmp_path, args, env, message):
    marker = tmp_path / "ran"
    result = _exec(tmp_path, *args, env=_child_env(**env),
                   script=f"open({str(marker)!r}, 'w').close()")
    assert result.returncode == exec_commands.EXIT_USAGE, result.stderr
    assert message in result.stderr
    assert not marker.exists()  # the program was not started
    assert result.stdout == ""  # and nothing fell back to console output


def test_to_wins_over_the_environment_and_the_environment_over_otel(tmp_path):
    from_env, from_flag = tmp_path / "env.jsonl", tmp_path / "flag.jsonl"
    env = _child_env(BRIEFCASE_EXPORTER=str(from_env),
                     OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:9")
    assert _exec(tmp_path, env=env).returncode == 0
    assert len(_records(from_env)) == 1
    assert _exec(tmp_path, "--to", str(from_flag), env=env).returncode == 0
    assert len(_records(from_flag)) == 1 and len(_records(from_env)) == 1


def test_dedupe_flag_reaches_the_child(tmp_path):
    out = tmp_path / "o.jsonl"
    result = _exec(tmp_path, "--to", str(out), "--dedupe", "provider",
                   script="import briefcase; print(briefcase.auto.status()['dedupe'])")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "provider"


# -- red team: the bootstrap ----------------------------------------------------

def _bootstrap_env(tmp_path, **extra):
    env = _child_env(BRIEFCASE_AUTO="1", **extra)
    env["PYTHONPATH"] = os.pathsep.join([BOOTSTRAP_DIR, env["PYTHONPATH"]])
    return env


def _python(tmp_path, code, env, *flags):
    return subprocess.run([sys.executable, *flags, "-c", textwrap.dedent(code)],
                          cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120)


def test_a_bad_destination_in_the_child_disables_capture_and_says_so(tmp_path):
    """Set after exec's own check (a child process with another environment):
    never a silent fallback to console output."""
    for extra, word in (({"BRIEFCASE_EXPORTER": "consol"}, "Unknown exporter shorthand"),
                        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "collector:4317"}, "not a valid http")):
        result = _python(tmp_path, """
            import briefcase
            from briefcase.config import BriefcaseConfig
            print(BriefcaseConfig.get().exporter, briefcase.auto.pending())
        """, _bootstrap_env(tmp_path, **extra))
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == ["None", "{}"]
        assert "auto-instrumentation is disabled for this process" in result.stderr
        assert word in result.stderr


def test_unknown_skip_names_are_reported_by_the_child(tmp_path):
    out = tmp_path / "o.jsonl"
    result = _python(tmp_path, "print('ok')", _bootstrap_env(
        tmp_path, BRIEFCASE_EXPORTER=str(out), BRIEFCASE_AUTO_SKIP="langchian, openai"))
    assert result.returncode == 0 and result.stdout.strip() == "ok"
    assert "ignoring unknown name 'langchian'" in result.stderr


def test_an_interpreter_that_cannot_import_briefcase_is_left_alone(tmp_path):
    """Another Python (or virtualenv) started by the program inherits the
    bootstrap directory. It must run untouched: one line on stderr, nothing
    added to its sys.path."""
    installed = tmp_path / "site-packages" / "briefcase" / "_bootstrap"
    installed.mkdir(parents=True)
    (installed / "sitecustomize.py").write_text(
        (Path(BOOTSTRAP_DIR) / "sitecustomize.py").read_text())
    (tmp_path / "site-packages" / "foreign_pkg.py").write_text("X = 1\n")
    # An installed briefcase would be importable here, so a stub that fails to import shadows it.
    shadow = tmp_path / "shadow" / "briefcase"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("raise ImportError('briefcase is not installed for this interpreter')\n")
    env = _child_env(BRIEFCASE_AUTO="1")
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path / "shadow"), str(installed)])
    result = subprocess.run(
        [sys.executable, "-c", "import sys\ntry:\n    import foreign_pkg\n    print('LEAKED')\n"
                               "except ImportError:\n    print('clean')"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "clean"
    assert "auto-instrumentation is disabled for this process" in result.stderr


def test_a_source_checkout_is_importable_without_staying_on_sys_path(tmp_path):
    env = _child_env(BRIEFCASE_AUTO="1", BRIEFCASE_EXPORTER=str(tmp_path / "o.jsonl"))
    env["PYTHONPATH"] = BOOTSTRAP_DIR  # the checkout root is NOT on the path
    result = _python(tmp_path, f"""
        import sys
        print("briefcase" in sys.modules, {str(REPO)!r} in sys.path)
        import briefcase.integrations.providers  # submodules still import
    """, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["True", "False"]


def test_the_environments_own_sitecustomize_is_what_import_returns(tmp_path):
    other = tmp_path / "site_dir"
    other.mkdir()
    (other / "sitecustomize.py").write_text("MARK = 'environment'\n")
    env = _bootstrap_env(tmp_path, BRIEFCASE_EXPORTER=str(tmp_path / "o.jsonl"))
    env["PYTHONPATH"] = os.pathsep.join([BOOTSTRAP_DIR, str(other), str(REPO)])
    result = _python(tmp_path, "import sitecustomize; print(sitecustomize.MARK)", env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "environment"


def test_warnings_are_visible_under_exec_unless_logging_is_configured(tmp_path):
    code = """
        import logging
        logging.getLogger("briefcase.test").warning("VISIBLE-WARNING")
    """
    env = _bootstrap_env(tmp_path, BRIEFCASE_EXPORTER=str(tmp_path / "o.jsonl"))
    assert "VISIBLE-WARNING" in _python(tmp_path, code, env).stderr
    assert "VISIBLE-WARNING" not in _python(
        tmp_path, code, dict(env, BRIEFCASE_LOG_LEVEL="ERROR")).stderr


CHILDREN = """
    import multiprocessing, os, subprocess, sys
    import briefcase

    @briefcase.capture
    def work(tag):
        return tag

    def child(tag):
        work(tag)

    if __name__ == "__main__":
        work("parent")
        for method in sys.argv[1:]:
            if method == "os.fork":
                pid = os.fork()
                if pid == 0:
                    work("os.fork")
                    sys.exit(0)
                os.waitpid(pid, 0)
            elif method == "subprocess":
                code = "import briefcase; briefcase.capture(lambda tag: tag)('subprocess')"
                subprocess.run([sys.executable, "-c", code], check=True)
            else:
                process = multiprocessing.get_context(method).Process(target=child, args=(method,))
                process.start()
                process.join()
                assert process.exitcode == 0
"""


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
@pytest.mark.parametrize("method", ["spawn", "fork", "forkserver", "os.fork", "subprocess"])
def test_child_processes_are_instrumented_exactly_once(tmp_path, method):
    """Every way a program starts a child Python: its records are exported,
    once. multiprocessing *fork* children leave through os._exit (no atexit)
    and used to lose everything still queued."""
    out = tmp_path / "o.jsonl"
    result = _exec(tmp_path, "--to", str(out), script=CHILDREN, script_args=[method],
                   env=_child_env(PYTHONWARNINGS="ignore::DeprecationWarning"))
    assert result.returncode == 0, result.stderr
    tags = sorted(r["inputs"]["args"] for r in _records(out))
    assert tags == sorted([f"('{method}',)", "('parent',)"])


def test_startup_cost_of_the_bootstrap_is_bounded(tmp_path):
    import time

    def best(env):
        timings = []
        for _ in range(5):
            started = time.perf_counter()
            assert _python(tmp_path, "pass", env).returncode == 0
            timings.append(time.perf_counter() - started)
        return min(timings)

    plain = best(_child_env())
    bootstrapped = best(_bootstrap_env(tmp_path, BRIEFCASE_EXPORTER=str(tmp_path / "o.jsonl")))
    # importing briefcase and installing the import hook; no SDK is imported
    assert bootstrapped - plain < 1.0, (plain, bootstrapped)
