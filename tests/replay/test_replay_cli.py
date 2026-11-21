"""`briefcase replay` command: plan, confirmation, exit codes, report and diff."""

import argparse
import json

import pytest

from briefcase.cli import replay_commands
from briefcase.replay import recorded as rr

from tests.replay.test_replay_records import chat_record


def cli(argv):
    parser = argparse.ArgumentParser(prog="briefcase")
    sub = parser.add_subparsers(dest="command")
    replay_commands.register(sub)
    args = parser.parse_args(argv)
    return args.func(args, None, None, None)


@pytest.fixture
def runs(tmp_path):
    path = tmp_path / "runs.jsonl"
    records = [chat_record(0), chat_record(1), chat_record(2, inputs={})]
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


class FakeLive:
    name = "auto"
    live = True
    made = 0

    def __init__(self, **kwargs):
        pass

    def check(self, call, overrides):
        return []

    def execute(self, call, overrides):
        FakeLive.made += 1
        text = "Lyon" if call.index == 1 else "Paris"
        return rr.Execution({"text": text, "tool_calls": []},
                            {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
                            50.0, "gpt-test", "stop")


@pytest.fixture
def live(monkeypatch):
    FakeLive.made = 0
    monkeypatch.setattr(rr, "AutoExecutor", FakeLive)
    return FakeLive


def test_group_and_help_example(capsys):
    assert replay_commands.GROUP == "verify"
    with pytest.raises(SystemExit):
        cli(["replay", "run", "--help"])
    out = capsys.readouterr().out
    assert "Example:\n  briefcase replay run" in out


def test_cassette_run_writes_report_and_diff_reads_it(runs, tmp_path, capsys):
    report = tmp_path / "report.jsonl"
    assert cli(["replay", "run", str(runs), "--executor", "cassette", "--out", str(report)]) == 0
    out = capsys.readouterr().out
    assert "no network" in out
    assert "Result: 2 matched, 0 changed, 1 not_replayable, 0 error" in out
    assert f"Next: briefcase replay diff {report}" in out
    rows = [json.loads(line) for line in report.read_text().splitlines()]
    assert [r["status"] for r in rows] == ["matched", "matched", "not_replayable"]

    assert cli(["replay", "diff", str(report)]) == 0
    out = capsys.readouterr().out
    assert "3 call(s): 2 matched, 0 changed, 1 not_replayable, 0 error" in out
    assert "content capture was off" in out


def test_explain(runs, capsys):
    assert cli(["replay", "explain", str(runs)]) == 0
    out = capsys.readouterr().out
    assert "2 of 3 record(s) can be replayed." in out
    assert "content capture was off" in out
    assert cli(["replay", "explain", str(runs), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["replayable"] == 2
    assert "Paris" not in json.dumps(data)  # explain is content-free


def test_live_run_without_a_terminal_refuses_and_calls_nothing(runs, live, capsys):
    assert cli(["replay", "run", str(runs)]) == 1
    err = capsys.readouterr().err
    assert "--yes" in err
    assert live.made == 0


def test_live_run_asks_and_respects_no(runs, live, monkeypatch, capsys):
    questions = []
    monkeypatch.setattr(replay_commands, "ASK", lambda q: questions.append(q) or "n")
    assert cli(["replay", "run", str(runs)]) == 1
    assert "Make 2 live call(s)" in questions[0]
    assert live.made == 0
    monkeypatch.setattr(replay_commands, "ASK", lambda q: "y")
    assert cli(["replay", "run", str(runs)]) == 0
    assert live.made == 2


def test_yes_skips_the_prompt_and_fail_on_change_gates(runs, live, capsys):
    assert cli(["replay", "run", str(runs), "--yes", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["status"]["changed"] == 1
    assert data["plan"]["per_model"] == {"gpt-test": 2}
    assert cli(["replay", "run", str(runs), "--yes", "--fail-on-change"]) == 1


def test_plan_only_and_max_calls(runs, live, capsys):
    assert cli(["replay", "run", str(runs), "--plan-only"]) == 0
    out = capsys.readouterr().out
    assert "calls to make        2" in out and "upper bound" in out
    assert cli(["replay", "run", str(runs), "--yes", "--max-calls", "1"]) == 1
    assert "over --max-calls 1" in capsys.readouterr().err
    assert live.made == 0


def test_overrides_are_parsed(runs, live, tmp_path, capsys):
    report = tmp_path / "r.jsonl"
    assert cli(["replay", "run", str(runs), "--yes", "--model", "gpt-x", "--param",
                "temperature=0.5", "--param", 'response_format={"type":"json_object"}',
                "--out", str(report)]) == 0
    row = json.loads(report.read_text().splitlines()[0])
    assert row["replay_model"] == "gpt-x"
    assert row["overrides"]["params"] == {"temperature": 0.5,
                                          "response_format": {"type": "json_object"}}


def test_missing_file_and_bad_param(tmp_path, capsys):
    assert cli(["replay", "run", str(tmp_path / "nope.jsonl")]) == 1
    assert "capture_content=True" in capsys.readouterr().err
    path = tmp_path / "x.jsonl"
    path.write_text("")
    assert cli(["replay", "run", str(path), "--param", "novalue"]) == 1


def test_status_hook(runs, capsys):
    assert replay_commands.status()["state"] == "off"
    cli(["replay", "run", str(runs), "--executor", "cassette"])
    state = replay_commands.status()
    assert state["state"] == "on" and "2 matched" in state["summary"]
