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


def test_main_no_command_prints_help(capsys):
    assert main([]) == 2
