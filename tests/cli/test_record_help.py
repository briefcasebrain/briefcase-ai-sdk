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


def test_piped_clarification_uses_the_original_path(monkeypatch, capsys):
    from io import StringIO

    from briefcase.cli.__main__ import main

    monkeypatch.setattr("sys.stdin", StringIO("Where is the JSONL for marker job-31?\n/tmp/my runs/a.jsonl\n"))
    assert main(["record-help"]) == 0
    result = capsys.readouterr().out
    assert result.count("What JSONL path") == 1
    assert "/tmp/my runs/a.jsonl" in result
    assert "job-31" in result


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
