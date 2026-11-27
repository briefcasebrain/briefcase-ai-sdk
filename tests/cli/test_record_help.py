"""The wheel and documentation render the same rules and session-local evidence."""
import json
from pathlib import Path

import pytest

from briefcase.cli.record_help_commands import _TABLE, route


CASES = json.loads((Path(__file__).parent / "fixtures/record_guidance_v1.json").read_text())
TABLE = json.loads(_TABLE.read_text())


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_shared_record_guidance(case):
    confirmation = case.get("confirmation")
    action, text = route(TABLE, case["question"], case.get("answer"), case.get("shell"), confirmation=confirmation)
    assert action == case["expected"]
    assert text == TABLE["actions"][action]["text"]
    assert "{" not in text and "}" not in text
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
    assert "the original JSONL file" in result and "/tmp/my runs/a.jsonl" not in result
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
    assert main(["record-help", question, "--source", source, "--result", result, "--reader", "me",
                 "--source-match", "original", "--target-match", "original", "--coverage", "complete"]) == 0
    by_flag = capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", StringIO(f"{question}\n{source} {result} me original original complete\n"))
    assert main(["record-help"]) == 0
    by_pipe = capsys.readouterr().out
    # A generic artifact may trigger an initial question; the explicit conclusion is identical.
    assert by_pipe.endswith(by_flag)
    assert "alpha.jsonl" not in by_flag and "beta.jsonl" not in by_flag
    assert ("You reported" in by_flag) == (result != "unverified")


def test_outcome_requires_explicit_complete_pair(monkeypatch, capsys):
    from io import StringIO

    from briefcase.cli.__main__ import main

    question = "Scan of rows.jsonl found no row matching the original marker spot-41."
    for reply in ("", "jsonl", "I confirm jsonl matched", "jsonl matched; storage matched", "storage unknown"):
        monkeypatch.setattr("sys.stdin", StringIO(question + "\n" + reply + "\n"))
        assert main(["record-help"]) == 0
        assert "You reported" not in capsys.readouterr().out
    assert main(["record-help", question, "--source", "jsonl"]) == 2
    captured = capsys.readouterr()
    assert "both --source and --result" in captured.err and "You reported" not in captured.out


def test_three_turn_pipe_accepts_source_result_after_clarification(monkeypatch, capsys):
    from io import StringIO

    from briefcase.cli.__main__ import main

    data = "Where is the JSONL for marker run-123?\naudit.jsonl\njsonl no-match me original original complete\n"
    monkeypatch.setattr("sys.stdin", StringIO(data))
    assert main(["record-help"]) == 0
    result = capsys.readouterr().out
    assert "You reported no match" in result
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

    terminal = StringIO("Where is the JSONL for marker visit-671?\naudit.jsonl\n"
                        "jsonl no-match me original original complete\n")
    monkeypatch.setattr(terminal, "isatty", lambda: True)
    monkeypatch.setattr(_ux, "is_interactive", lambda: True)
    monkeypatch.setattr("sys.stdin", terminal)
    assert main(["record-help"]) == 0
    output = capsys.readouterr().out
    assert "Report your lookup or end input" in output and "You reported no match" in output
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
    assert "You reported" not in captured.out and "visit-671" not in captured.err + captured.out


def test_terminal_confirmation_after_answer_option(monkeypatch, capsys):
    from io import StringIO

    from briefcase.cli import _ux
    from briefcase.cli.__main__ import main

    terminal = StringIO("jsonl no-match me original original complete\n")
    monkeypatch.setattr(terminal, "isatty", lambda: True)
    monkeypatch.setattr(_ux, "is_interactive", lambda: True)
    monkeypatch.setattr("sys.stdin", terminal)
    assert main(["record-help", "Where is the JSONL for marker visit-671?", "--answer", "audit.jsonl"]) == 0
    result = capsys.readouterr().out
    assert "You reported no match" in result and "visit-671" not in result and "audit.jsonl" not in result


@pytest.mark.parametrize("option", ["--static=violet-892", "--help=violet-892"])
def test_malformed_option_uses_fixed_diagnostic(option, capsys):
    from briefcase.cli.__main__ import main

    with pytest.raises(SystemExit) as exc:
        main(["record-help", "My marker was violet-892.", option])
    assert exc.value.code == 2
    result = capsys.readouterr()
    assert result.out == ""
    assert result.err == "Invalid record-help arguments. See record-help --help.\n"


@pytest.mark.parametrize("lines,code,phrase", [
    ("jsonl matched\njsonl no-match\n", 2, "once"),
    ("A question\nreceiver matched\nstorage matched\n", 2, "once"),
    ("Explicit JSONL to audit.jsonl.\nI reopened it after exit.\n"
     "jsonl no-match me original original complete\n", 0, "You reported no match"),
])
def test_final_report_is_decoded_independently_of_question(lines, code, phrase, monkeypatch, capsys):
    from io import StringIO
    from briefcase.cli.__main__ import main

    monkeypatch.setattr("sys.stdin", StringIO(lines))
    assert main(["record-help"]) == code
    result = capsys.readouterr()
    assert phrase in result.out + result.err
    assert "audit.jsonl" not in result.out + result.err


@pytest.mark.parametrize("inline,expected", [
    ("", "disabled Node preload"), ("BRIEFCASE_AUTO=1 ", "original JSONL file")
])
def test_bare_preload_honors_inherited_activation(inline, expected, monkeypatch, capsys):
    from io import StringIO
    from briefcase.cli.__main__ import main

    monkeypatch.setattr("sys.stdin", StringIO(""))
    monkeypatch.setenv("BRIEFCASE_AUTO", "0")
    monkeypatch.setenv("BRIEFCASE_EXPORTER", "audit.jsonl")
    q = "The Node preload inherited the unchanged settings from this shell. "
    assert main(["record-help", q + inline + "node --import @briefcase-ai/auto/register app.js"]) == 0
    result = capsys.readouterr()
    assert expected in result.out and "audit.jsonl" not in result.out + result.err


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("second", ["jsonl matched", "storage no-match", "receiver unverified"])
def test_two_reports_before_a_question_are_rejected(terminal, second, monkeypatch, capsys):
    from io import StringIO
    from briefcase.cli import _ux
    from briefcase.cli.__main__ import main

    stream = StringIO(f"jsonl matched\n{second}\n")
    monkeypatch.setattr(stream, "isatty", lambda: terminal)
    monkeypatch.setattr(_ux, "is_interactive", lambda: terminal)
    monkeypatch.setattr("sys.stdin", stream)
    assert main(["record-help"]) == 2
    captured = capsys.readouterr()
    assert "once" in captured.err and "You reported" not in captured.out


@pytest.mark.parametrize("answer", ["opaque-value", "jsonl matched", "storage no-match"])
def test_repeated_answer_options_are_rejected(answer, capsys):
    from briefcase.cli.__main__ import main

    assert main(["record-help", "Which file?", "--answer", answer, "--answer", "local.jsonl"]) == 2
    captured = capsys.readouterr()
    assert captured.err == "Supply one clarification answer, once.\n"
    assert captured.out == ""


def test_all_record_help_text_is_ascii():
    texts = [TABLE["guide"], TABLE["confirmation"]["instruction"]]
    for text in texts + [item["text"] for item in TABLE["actions"].values()]:
        assert text.isascii()


@pytest.mark.parametrize("debug", [False, True])
def test_record_help_internal_failure_has_fixed_diagnostic(debug, monkeypatch, capsys):
    from briefcase.cli import record_help_commands
    from briefcase.cli.__main__ import main

    def fail(*args):
        raise RuntimeError("supplied-free-text")

    monkeypatch.setattr(record_help_commands, "_guide_session", fail)
    assert main((["--debug"] if debug else []) + ["record-help", "violet-892"]) == 1
    captured = capsys.readouterr()
    assert "supplied-free-text" not in captured.out + captured.err and "violet-892" not in captured.out + captured.err
    assert "Retry briefcase-ai record-help --static." in captured.err


@pytest.mark.parametrize("reset", ['BRIEFCASE_EXPORTER=""', "BRIEFCASE_EXPORTER=''", "BRIEFCASE_EXPORTER="])
def test_empty_inline_assignment_resets_inherited_file(reset, monkeypatch, capsys):
    from io import StringIO
    from briefcase.cli.__main__ import main

    monkeypatch.setattr("sys.stdin", StringIO(""))
    monkeypatch.setenv("BRIEFCASE_EXPORTER", "prior.jsonl")
    for key in TABLE["inlineNode"]["endpoints"]:
        monkeypatch.delenv(key, raising=False)
    q = "The Node preload inherited the unchanged settings from this shell. "
    assert main(["record-help", q + reset + " node --import @briefcase-ai/auto/register app.js"]) == 0
    text = capsys.readouterr().out
    assert "retained stderr" in text and "original JSONL file" not in text


@pytest.mark.parametrize("first,second", [("console", "OTLP"), ("jsonl", "memory"), ("unknown", "console")])
def test_flags_and_pipe_cannot_disagree_on_clarification(monkeypatch, capsys, first, second):
    from io import StringIO
    from briefcase.cli.__main__ import main

    monkeypatch.setattr("sys.stdin", StringIO(second + "\n"))
    assert main(["record-help", "Which exporter did my original process use?", "--answer", first]) == 2
    capture = capsys.readouterr()
    assert capture.out == "" and capture.err == "Supply one clarification answer, once.\n"


@pytest.mark.parametrize("gap", ["", "\n", "\n \n"])
@pytest.mark.parametrize("second", ["receiver matched", "storage no-match"])
@pytest.mark.parametrize("question", ["My original run used OTLP.", "Which exporter did my original process use?"])
def test_terminal_rejects_pasted_second_report(monkeypatch, capsys, question, second, gap):
    from io import StringIO
    from briefcase.cli import _ux
    from briefcase.cli.__main__ import main

    terminal = StringIO(f"receiver matched\n{gap}{second}\n")
    monkeypatch.setattr(terminal, "isatty", lambda: True)
    monkeypatch.setattr(_ux, "is_interactive", lambda: True)
    monkeypatch.setattr("sys.stdin", terminal)
    assert main(["record-help", question]) == 2
    capture = capsys.readouterr()
    assert "You reported" not in capture.out
    assert "once" in capture.err


@pytest.mark.parametrize("pair", [
    "receiver matched me original original", "receiver no-match me original original complete", "",
])
def test_terminal_investigation_precedes_optional_outcome(monkeypatch, capsys, pair):
    from io import StringIO
    from briefcase.cli import _ux
    from briefcase.cli.__main__ import main

    terminal = StringIO(pair + "\n")
    monkeypatch.setattr(terminal, "isatty", lambda: True)
    monkeypatch.setattr(_ux, "is_interactive", lambda: True)
    monkeypatch.setattr("sys.stdin", terminal)
    assert main(["record-help", "My original run used OTLP."]) == 0
    capture = capsys.readouterr().out
    assert capture.count("Search the collector") == 1
    assert capture.index("Search the collector") < capture.index("Report your lookup or end input")
    if pair:
        assert capture.index("Report your lookup or end input") < capture.index("You reported")


@pytest.mark.parametrize("command", ["", " node --import @briefcase-ai/auto/register app.js"])
@pytest.mark.parametrize("env,expected", [
    ({"BRIEFCASE_AUTO": "0", "BRIEFCASE_EXPORTER": "prior.jsonl"}, "disabled Node preload"),
    ({"BRIEFCASE_AUTO": "False", "BRIEFCASE_EXPORTER": "otlp"}, "disabled Node preload"),
    ({"BRIEFCASE_EXPORTER": "otlp", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://999.999.999.999:4318"},
     "startup diagnostic"),
    ({"BRIEFCASE_EXPORTER": "otlp", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4318"}, "Search the collector"),
])
def test_node_preload_resolution_is_shared_with_terminal_history(monkeypatch, capsys, command, env, expected):
    from io import StringIO
    from briefcase.cli.__main__ import main

    for name in (*TABLE["inlineNode"]["keys"], "NODE_OPTIONS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("sys.stdin", StringIO(""))
    question = "The original Node preload inherited the unchanged settings from this shell."
    assert main(["record-help", question + command]) == 0
    assert expected in capsys.readouterr().out


@pytest.mark.parametrize("flag,command,expected", [
    ("", "", "disabled Node preload"),
    ("1", "", "original JSONL file"),
    ("", " node --import @briefcase-ai/auto/register app.js", "original JSONL file"),
    ("0", " node --import @briefcase-ai/auto/register app.js", "disabled Node preload"),
])
def test_preload_activation_from_node_options(monkeypatch, capsys, flag, command, expected):
    from io import StringIO
    from briefcase.cli.__main__ import main

    monkeypatch.setenv("NODE_OPTIONS", "--import @briefcase-ai/auto/register")
    monkeypatch.setenv("BRIEFCASE_AUTO", flag)
    monkeypatch.setenv("BRIEFCASE_EXPORTER", "example.jsonl")
    monkeypatch.setattr("sys.stdin", StringIO(""))
    q = "The original Node preload inherited the unchanged settings from this shell."
    assert main(["record-help", q + command]) == 0
    assert expected in capsys.readouterr().out


@pytest.mark.parametrize("arguments,expected", [
    ("--no-warnings --import @briefcase-ai/auto/register script.js", True),
    ("--conditions development --require @briefcase-ai/auto/register.js script.js", False),
    ("--title '--import @briefcase-ai/auto/register' script.js", False),
    ("--no-warnings script.js --import @briefcase-ai/auto/register", False),
    ("-- --import @briefcase-ai/auto/register", False),
    ("--import @briefcase-ai/auto/register-sample", False),
    ("--import '@briefcase-ai/auto/reg\"ister'", False),
])
def test_only_runtime_options_activate_the_original_preload(arguments, expected):
    from briefcase.cli.record_help_commands import _node_register

    assert (_node_register(TABLE["inlineNode"], " " + arguments) == "register") is expected


@pytest.mark.parametrize("case", [
    case for case in CASES if not any(key in case for key in ("answer", "shell", "confirmation"))
    and ("node " in case["question"] or "exec --" in case["question"] or "called observe(" in case["question"])
    and case["expected"] in {"node_startup", "preload_startup", "wrapper_startup", "console", "node_console",
                             "node_file", "memory_only", "inactive_preload", "otlp", "receiver", "invalid_web"}
    and ("--import" in case["question"] or "--require" in case["question"]
         or "called observe(" in case["question"] or "exec --" in case["question"])
], ids=lambda c: c["name"])
def test_original_execution_evidence_is_position_independent(case):
    original = case["question"]
    actual = route(TABLE, "Which exporter did my original process use?", answer=original)
    assert actual == route(TABLE, original)


@pytest.mark.parametrize("source", ["jsonl", "receiver", "storage"])
@pytest.mark.parametrize("result", ["matched", "no-match", "unverified"])
@pytest.mark.parametrize("channel", ["flags", "pipe", "terminal"])
def test_foreign_observation_cannot_confirm_original(source, result, channel, monkeypatch, capsys):
    from io import StringIO
    from briefcase.cli import _ux
    from briefcase.cli.__main__ import main

    # Keeping the contrary observation in a separate turn exercises the session decoder.
    first = "Where is the JSONL for the original decision?"
    followup = f"local-file.jsonl. The {source} result I am reporting is for a different run."
    args = ["record-help", first]
    if channel == "flags":
        args.extend(["--answer", followup, "--source", source, "--result", result])
        turns = ""
    elif channel == "terminal":
        args.extend(["--answer", "local-file.jsonl"])
        args[1] += f" The {source} result I am reporting is for a different run."
        turns = f"{source} {result}\n"
    else:
        turns = followup + f"\n{source} {result}\n"
    stream = StringIO(turns)
    monkeypatch.setattr(stream, "isatty", lambda: channel == "terminal")
    monkeypatch.setattr(_ux, "is_interactive", lambda: channel == "terminal")
    monkeypatch.setattr("sys.stdin", stream)
    assert main(args) == 0
    emitted = capsys.readouterr()
    assert "result is for a different run" in emitted.out
    assert "You reported" not in emitted.out + emitted.err


def test_chained_sqlite_precedes_jsonl_extension_in_public_resolver(monkeypatch):
    from unittest.mock import Mock
    from briefcase import _observe
    from briefcase.exporters import chained

    factory = Mock()
    monkeypatch.setattr(chained, "ChainedSqliteExporter", factory)
    assert _observe._resolve_exporter("chain:records.jsonl") is factory.return_value
    factory.assert_called_once_with("records.jsonl")
    statement = 'Before recording the decision, the original Python application called observe("chain:records.jsonl").'
    assert route(TABLE, statement)[0] == "chain_store"


@pytest.mark.parametrize("extra,phrase", [
    ("", "Search the intended source"),
    ("another", "Search the intended source yourself"),
    ("me different", "You checked a different file"),
    ("me original different", "different run"),
    ("me original original partial", "A complete search is still unverified"),
    ("me original original complete", "You reported no match"),
])
def test_progressive_observation_report_in_flags_and_pipe(extra, phrase, monkeypatch, capsys):
    from io import StringIO
    from briefcase.cli.__main__ import main

    question = "How do I check the original JSONL file?"
    values = ["jsonl", "no-match", *extra.split()]
    options = ["--source", "--result", "--reader", "--source-match", "--target-match", "--coverage"]
    args = [word for pair in zip(options, values) for word in pair]
    monkeypatch.setattr("sys.stdin", StringIO(""))
    assert main(["record-help", question, *args]) == 0
    flag_text = capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", StringIO(question + "\n" + " ".join(values)))
    assert main(["record-help"]) == 0
    pipe_text = capsys.readouterr().out
    assert phrase.lower() in flag_text.lower() and pipe_text.endswith(flag_text)


@pytest.mark.parametrize("extra", ["--reader", "--source-match", "--target-match", "--coverage"])
def test_observation_rejects_unknown_option_value_without_echo(extra, capsys):
    from briefcase.cli.__main__ import main

    assert main(["record-help", "--source", "receiver", "--result", "matched", extra, "private-value"]) == 2
    captured = capsys.readouterr()
    assert "private-value" not in captured.out + captured.err and "You reported" not in captured.out


def test_static_receiver_observation_example_is_executable(capsys):
    import re
    import shlex
    from briefcase.cli.__main__ import main

    assert main(["record-help", "--static"]) == 0
    guide = capsys.readouterr().out
    example = re.search(r"use (--source receiver .*?)\.", guide)
    assert example is not None
    assert main(["record-help", *shlex.split(example.group(1))]) == 0
    assert "You reported a matching entry" in capsys.readouterr().out


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("tail", ["", "\n", "storage matched me original original\n",
                                  "\nstorage no-match me original original complete\n"])
def test_optional_lookup_details_precede_one_final_observation(terminal, tail, monkeypatch, capsys):
    from io import StringIO
    from briefcase.cli import _ux
    from briefcase.cli.__main__ import main

    session = ("I queried the intended downstream store for the original decision ID.\n"
               "The query returned a full record with that same ID.\n"
               "storage matched me original original\n" + tail)
    stream = StringIO(session)
    monkeypatch.setattr(stream, "isatty", lambda: terminal)
    monkeypatch.setattr(_ux, "is_interactive", lambda: terminal)
    monkeypatch.setattr("sys.stdin", stream)
    assert main(["record-help"]) == (2 if tail.strip() else 0)
    output = capsys.readouterr()
    assert output.out.count("You reported") == (0 if tail.strip() else 1)


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("extra", ["", "\nstorage matched me original original\n"])
@pytest.mark.parametrize("gap", ["\n", "\n\n", "\n \n\t\n"])
def test_blank_before_optional_report_preserves_full_session(terminal, extra, gap, monkeypatch, capsys):
    from io import StringIO
    from briefcase.cli import _ux
    from briefcase.cli.__main__ import main

    stream = StringIO("I queried the intended downstream store for the original decision ID.\n" + gap
                      + "storage matched me original original\n" + extra)
    monkeypatch.setattr(stream, "isatty", lambda: terminal)
    monkeypatch.setattr(_ux, "is_interactive", lambda: terminal)
    monkeypatch.setattr("sys.stdin", stream)
    assert main(["record-help"]) == (2 if extra else 0)
    assert capsys.readouterr().out.count("You reported") == (0 if extra else 1)


def test_terminal_applies_optional_details_before_collecting_outcome(monkeypatch, capsys):
    from io import StringIO
    from briefcase.cli import _ux
    from briefcase.cli.__main__ import main

    stream = StringIO("I queried the intended downstream store for the original decision ID.\n"
                      "The query returned a full record with that same ID.\nstorage matched me original original\n")
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setattr(_ux, "is_interactive", lambda: True)
    monkeypatch.setattr("sys.stdin", stream)
    assert main(["record-help"]) == 0
    output = capsys.readouterr().out
    assert output.index(TABLE["actions"]["stored"]["text"]) < output.index("Confirm what you found")
    assert output.index("Confirm what you found") < output.index(TABLE["actions"]["confirmed_storage_matched"]["text"])


@pytest.mark.parametrize("details", ["", "The query returned a full record with that same ID.\n"])
def test_one_eof_finishes_optional_observation(monkeypatch, capsys, details):
    from io import StringIO
    from briefcase.cli import _ux
    from briefcase.cli.__main__ import main

    class Session(StringIO):
        ended = False

        def readline(self, *args, **kwargs):
            assert not self.ended, "read repeated after EOF"
            line = super().readline(*args, **kwargs)
            self.ended = line == ""
            return line

        def __iter__(self):
            assert not self.ended, "read repeated after EOF"
            return super().__iter__()

    stream = Session(details)
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setattr(_ux, "is_interactive", lambda: True)
    monkeypatch.setattr("sys.stdin", stream)
    question = "I queried the intended downstream store for the original decision ID."
    assert main(["record-help", question]) == 0
    assert stream.ended and "You reported" not in capsys.readouterr().out
