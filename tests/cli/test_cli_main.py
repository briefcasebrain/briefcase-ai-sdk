"""Parser wiring + main() dispatch, including the global --dry-run happy path."""
import pytest

from briefcase.cli.__main__ import build_parser, main


def test_parser_accepts_full_surface():
    p = build_parser()
    # a representative invocation from each command group parses without error
    p.parse_args(["dataset", "register", "xor", "--uri", "synthetic://xor"])
    p.parse_args(["dataset", "list"])
    p.parse_args(["secret", "set", "K=V"])
    p.parse_args(["secret", "list"])
    p.parse_args([
        "run", "submit", "demo", "--repository", "cand", "--dataset", "xor",
        "--checkpoint", "base", "--metric", "f1", "--mode", "gate",
        "--secret", "T=1", "--env", "K=V",
    ])
    for sub in ("list",):
        p.parse_args(["run", sub])
    for sub in ("inspect", "logs", "results", "stop", "delete"):
        p.parse_args(["run", sub, "demo"])


def test_help_exits_zero():
    with pytest.raises(SystemExit) as e:
        build_parser().parse_args(["--help"])
    assert e.value.code == 0


def test_main_dry_run_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIEFCASE_HOME", str(tmp_path))
    # Pin the subprocess engine: this asserts the resolved *oci-jj argv* is printed, which is the
    # cli engine's dry-run form (the default 'auto' would print the gRPC RpcCall form instead).
    monkeypatch.setenv("BRIEFCASE_ENGINE", "cli")
    assert main(["dataset", "register", "xor", "--uri", "synthetic://xor"]) == 0
    rc = main([
        "--dry-run", "run", "submit", "demo",
        "--repository", "cand", "--dataset", "xor", "--checkpoint", "base",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "attach-bench" in out  # the resolved oci-jj argv was printed, not executed


def test_main_no_command_prints_the_command_list_and_status(shell_home, capsys):
    # Without a terminal a bare `briefcase-ai` never prompts and never fails a script: it prints
    # the grouped command list and the status screen (see tests/cli/test_cli_shell.py).
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "Start here:" in out and "briefcase-ai status" in out


TAGLINE = ("Compliance monitoring, auditing, and evaluating third-party artificial intelligence "
           "systems and models.")


def _tty(monkeypatch):
    from briefcase.cli import _ux

    monkeypatch.setattr(_ux, "is_tty", lambda stream=None: True)


def test_version_names_the_mark(capsys):
    import briefcase

    with pytest.raises(SystemExit):
        main(["--version"])
    assert capsys.readouterr().out.strip() == f"Briefcase AI {briefcase.__version__}"


def test_bare_briefcase_opens_with_the_banner_on_a_terminal(tmp_path, monkeypatch, capsys):
    import briefcase

    monkeypatch.setenv("BRIEFCASE_HOME", str(tmp_path))
    _tty(monkeypatch)
    assert main(["--no-input"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == [f"Briefcase AI {briefcase.__version__}", TAGLINE]


def test_help_opens_with_the_banner_on_a_terminal(monkeypatch, capsys):
    import briefcase

    _tty(monkeypatch)
    with pytest.raises(SystemExit):
        main(["--help"])
    assert capsys.readouterr().out.splitlines()[:2] == [f"Briefcase AI {briefcase.__version__}", TAGLINE]


def test_no_banner_when_output_is_not_a_terminal(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIEFCASE_HOME", str(tmp_path))
    assert main(["--no-input"]) == 0
    assert "Briefcase AI" not in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "Briefcase AI" not in capsys.readouterr().out


def test_parsed_output_never_carries_the_banner_even_on_a_terminal(tmp_path, monkeypatch, capsys):
    import json

    monkeypatch.setenv("BRIEFCASE_HOME", str(tmp_path))
    _tty(monkeypatch)
    assert main(["status", "--json"]) == 0
    assert isinstance(json.loads(capsys.readouterr().out), dict)
    assert main(["features", "--json"]) == 0
    json.loads(capsys.readouterr().out)


def test_help_description_opens_with_the_owner_sentence_on_its_own_line():
    lines = build_parser().format_help().splitlines()
    usage_end = next(i for i, line in enumerate(lines) if not line.strip())
    assert lines[usage_end + 1] == TAGLINE


def test_secret_list_says_so_on_a_terminal_when_nothing_is_stored(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIEFCASE_HOME", str(tmp_path))
    _tty(monkeypatch)
    assert main(["secret", "list"]) == 0
    assert capsys.readouterr().out == "no secrets stored\n"


def test_secret_list_output_is_unchanged_when_piped_or_json(tmp_path, monkeypatch, capsys):
    import json

    monkeypatch.setenv("BRIEFCASE_HOME", str(tmp_path))
    assert main(["secret", "list"]) == 0
    assert capsys.readouterr().out == ""
    _tty(monkeypatch)
    assert main(["secret", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
