"""The wheel and documentation render the same rules and session-local evidence."""
import json
from pathlib import Path

import pytest

from briefcase.cli.record_help_commands import _TABLE, route


CASES = json.loads((Path(__file__).parent / "fixtures/record_guidance_v1.json").read_text())
TABLE = json.loads(_TABLE.read_text())


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_shared_record_guidance(case):
    pair = case.get("confirmation")
    confirmation = (pair["source"], pair["result"]) if pair else None
    action, text = route(TABLE, case["question"], case.get("answer"), case.get("shell"), confirmation=confirmation)
    assert action == case["expected"]
    for fragment in case["includes"]:
        assert fragment in text
    for value in case.get("omits", []):
        assert value.casefold() not in text.casefold()


def test_piped_clarification_uses_the_original_path(monkeypatch, capsys):
    from io import StringIO

    from briefcase.cli.__main__ import main

    monkeypatch.setattr("sys.stdin", StringIO("Where is the JSONL for marker job-31?\n/tmp/my runs/a.jsonl\n"))
    assert main(["record-help"]) == 0
    result = capsys.readouterr().out
    assert result.count("What JSONL path") == 1
    assert "/tmp/my runs/a.jsonl" in result
    assert "job-31" not in result


def test_unknown_displays_same_guide_as_static(monkeypatch, capsys):
    from io import StringIO

    from briefcase.cli.__main__ import main

    monkeypatch.setattr("sys.stdin", StringIO(""))
    assert main(["record-help", "--static"]) == 0
    static = capsys.readouterr().out
    assert main(["record-help", "Which JSONL file?", "--answer", "unknown"]) == 0
    assert capsys.readouterr().out.endswith(static)


def test_url_credentials_never_appear_in_piped_result(monkeypatch, capsys):
    from io import StringIO

    from briefcase.cli.__main__ import main

    monkeypatch.setattr("sys.stdin", StringIO(
        "The exporter used https://synthetic:demo'fixture-secret@example.invalid/archive.jsonl?key=private\n"
    ))
    assert main(["record-help"]) == 0
    result = capsys.readouterr().out
    assert "receiver" in result
    assert "fixture-secret" not in result
    assert "key=private" not in result


def _paraphrases():
    from itertools import product

    templates = Path(__file__).parent / "fixtures/record_guidance_variants_v1.json"
    for template in json.loads(templates.read_text()):
        axes = template["variants"]
        for values in product(*axes.values()):
            slots = dict(zip(axes, values))
            yield {
                "name": template["name"] + " " + "/".join(values),
                "question": template["question"].format_map(slots),
                "answer": template.get("answer", "").format_map(slots) or None,
                "expected": template["expected"],
                "includes": [s.format_map(slots) for s in template["includes"]],
                "omits": [s.format_map(slots) for s in template.get("omits", [])],
            }


@pytest.mark.parametrize("case", list(_paraphrases()), ids=lambda row: row["name"])
def test_paraphrases_keep_the_same_action_and_artifacts(case):
    test_shared_record_guidance(case)


@pytest.mark.parametrize("source", ["jsonl", "receiver", "storage"])
@pytest.mark.parametrize("result", ["matched", "no-match", "unverified"])
def test_confirmation_flags_and_pipe_report_only_selected_source(source, result, monkeypatch, capsys):
    from io import StringIO

    from briefcase.cli.__main__ import main

    question = "I opened alpha.jsonl, then beta.jsonl. A receiver returned a row."
    monkeypatch.setattr("sys.stdin", StringIO(""))
    assert main(["record-help", question, "--source", source, "--result", result]) == 0
    by_flag = capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", StringIO(f"{question}\n{source} {result}\n"))
    assert main(["record-help"]) == 0
    by_pipe = capsys.readouterr().out
    # A generic artifact may trigger an initial question; the explicit conclusion is identical.
    assert by_pipe.endswith(by_flag)
    assert "alpha.jsonl" not in by_flag and "beta.jsonl" not in by_flag
    assert ("Reader-confirmed" in by_flag) == (result != "unverified")


def test_outcome_requires_explicit_complete_pair(monkeypatch, capsys):
    from io import StringIO

    from briefcase.cli.__main__ import main

    question = "Scan of rows.jsonl found no row matching the original marker spot-41."
    for reply in ("", "jsonl", "I confirm jsonl matched", "jsonl matched; storage matched", "storage unknown"):
        monkeypatch.setattr("sys.stdin", StringIO(question + "\n" + reply + "\n"))
        assert main(["record-help"]) == 0
        assert "Reader-confirmed" not in capsys.readouterr().out
    assert main(["record-help", question, "--source", "jsonl"]) == 2
    captured = capsys.readouterr()
    assert "both --source and --result" in captured.err and "Reader-confirmed" not in captured.out


def test_three_turn_pipe_accepts_source_result_after_clarification(monkeypatch, capsys):
    from io import StringIO

    from briefcase.cli.__main__ import main

    monkeypatch.setattr("sys.stdin", StringIO("Where is the JSONL for marker run-123?\naudit.jsonl\njsonl no-match\n"))
    assert main(["record-help"]) == 0
    result = capsys.readouterr().out
    assert "Reader-confirmed absence" in result
    assert "audit.jsonl" not in result and "Open audit.jsonl" not in result


@pytest.mark.parametrize("question,missing", [
    ("The console exporter was used. Stdout was redirected to app.err. I did not retain stderr.", "app.err"),
    ("The console exporter was used. python app.py 2>&1 > after.log", "after.log"),
    ("The console exporter was used. python app.py > merged.log 2>&1", "&1"),
])
def test_console_stream_does_not_swap_descriptors(question, missing):
    assert missing not in route(TABLE, question)[1]


def test_terminal_accepts_confirmation_after_path_clarification(monkeypatch, capsys):
    from io import StringIO

    from briefcase.cli import _ux
    from briefcase.cli.__main__ import main

    terminal = StringIO("Where is the JSONL for marker visit-671?\naudit.jsonl\njsonl no-match\n")
    monkeypatch.setattr(terminal, "isatty", lambda: True)
    monkeypatch.setattr(_ux, "is_interactive", lambda: True)
    monkeypatch.setattr("sys.stdin", terminal)
    assert main(["record-help"]) == 0
    output = capsys.readouterr().out
    assert "Optional source and result" in output and "Reader-confirmed absence" in output
    assert "audit.jsonl" not in output


@pytest.mark.parametrize("flag_pair", [True, False])
@pytest.mark.parametrize("last", ["jsonl matched", "jsonl no-match"])
def test_duplicate_and_conflicting_session_reports_are_rejected(flag_pair, last, monkeypatch, capsys):
    from io import StringIO

    from briefcase.cli.__main__ import main

    question = "Where is the JSONL for marker visit-671?"
    args = ["record-help", question]
    if flag_pair:
        args.extend(["--source", "jsonl", "--result", "matched"])
        data = last
    else:
        data = "jsonl matched\n" + last
    monkeypatch.setattr("sys.stdin", StringIO(data + "\n"))
    assert main(args) == 2
    captured = capsys.readouterr()
    assert "once" in captured.err
    assert "Reader-confirmed" not in captured.out and "visit-671" not in captured.err + captured.out


def test_terminal_confirmation_after_answer_option(monkeypatch, capsys):
    from io import StringIO

    from briefcase.cli import _ux
    from briefcase.cli.__main__ import main

    terminal = StringIO("jsonl no-match\n")
    monkeypatch.setattr(terminal, "isatty", lambda: True)
    monkeypatch.setattr(_ux, "is_interactive", lambda: True)
    monkeypatch.setattr("sys.stdin", terminal)
    assert main(["record-help", "Where is the JSONL for marker visit-671?", "--answer", "audit.jsonl"]) == 0
    result = capsys.readouterr().out
    assert "Reader-confirmed absence" in result and "visit-671" not in result and "audit.jsonl" not in result
