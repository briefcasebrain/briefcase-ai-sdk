"""The guidance command reads configuration without creating an exporter."""
import pytest

from briefcase.cli.record_help_commands import _shell_exporter


@pytest.mark.parametrize("question,env,expected", [
    ("original Python process", {"BRIEFCASE_EXPORTER": "otel", "OTEL_SDK_DISABLED": "TRUE"}, "disabled"),
    ("original Node process", {"BRIEFCASE_EXPORTER": "otlp", "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}, "console"),
    ("original Node process", {"BRIEFCASE_EXPORTER": "otel", "OTEL_SDK_DISABLED": "true"}, "otlp"),
    ("original Python process", {"BRIEFCASE_EXPORTER": "console", "OTEL_SDK_DISABLED": "true"}, "console"),
    ("the job", {"BRIEFCASE_EXPORTER": "otlp", "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}, "check-launch-config"),
    ("original Node process", {"BRIEFCASE_EXPORTER": "otlp", "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
                               "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/json"}, "otlp"),
    ("original Python process", {"BRIEFCASE_EXPORTER": "custom:secret.jsonl"}, "check-launch-config"),
    ("Node worker thread", {"BRIEFCASE_EXPORTER": "old.jsonl"}, "check-launch-config"),
    ("Python", {"BRIEFCASE_EXPORTER": "https://user:secret@example.org/file.jsonl"}, "HTTP"),
    ("Node", {"BRIEFCASE_EXPORTER": "http://receiver.example/decisions"}, "check-launch-config"),
    ("Python", {"BRIEFCASE_EXPORTER": "http://receiver.example/decisions"}, "check-launch-config"),
    ("Node", {"BRIEFCASE_EXPORTER": "http://[::1]:4318/decisions"}, "HTTP"),
    ("Python", {"BRIEFCASE_EXPORTER": "https://server:99999/"}, "check-launch-config"),
    ("Node", {"OTEL_EXPORTER_OTLP_ENDPOINT": "invalid-url"}, "check-launch-config"),
])
def test_inherited_configuration(question, env, expected, monkeypatch):
    for key in ("BRIEFCASE_EXPORTER", "OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_SDK_DISABLED"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert _shell_exporter(question) == expected


@pytest.mark.parametrize("current", [None, "laptop.jsonl"])
def test_local_exporter_value_requires_this_shell(monkeypatch, capsys, current):
    from io import StringIO

    from briefcase.cli.__main__ import main

    if current is None:
        monkeypatch.delenv("BRIEFCASE_EXPORTER", raising=False)
    else:
        monkeypatch.setenv("BRIEFCASE_EXPORTER", current)
    monkeypatch.setattr("sys.stdin", StringIO(""))
    question = ("The Node preload inherited the unchanged settings. stderr was saved in monitoring.err. "
                "I need usage metadata.")
    assert main(["record-help", question]) == 0
    text = capsys.readouterr().out
    assert "Which exporter did the original process use?" in text
    assert "laptop.jsonl" not in text


def test_other_runtime_cannot_decide_original_shell_configuration(monkeypatch, capsys):
    from io import StringIO

    from briefcase.cli.__main__ import main

    monkeypatch.setenv("BRIEFCASE_EXPORTER", "otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    monkeypatch.setattr("sys.stdin", StringIO(""))
    question = ("The original job used the default exporter from this shell with unchanged settings. "
                "A different Node app used console.")
    assert main(["record-help", question]) == 0
    assert "original launch configuration" in capsys.readouterr().out


@pytest.mark.parametrize("inspection", [
    "I use Node to inspect the output.",
    "Node is my tool for reviewing the logs.",
    "I use a Node dashboard.",
])
def test_inspection_tool_does_not_identify_original_runtime(monkeypatch, capsys, inspection):
    from io import StringIO

    from briefcase.cli.__main__ import main

    monkeypatch.setenv("BRIEFCASE_EXPORTER", "otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    monkeypatch.setattr("sys.stdin", StringIO(""))
    question = "The original job used the default exporter from this shell with unchanged settings. " + inspection
    assert main(["record-help", question]) == 0
    assert "original launch configuration" in capsys.readouterr().out


@pytest.mark.parametrize("runtime", [
    "It was not launched with Node.",
    "It might have been launched with Node.",
    "I am unsure whether the original Node process ran.",
])
def test_nonaffirmative_runtime_cannot_resolve_default(monkeypatch, capsys, runtime):
    from io import StringIO

    from briefcase.cli.__main__ import main

    monkeypatch.setenv("BRIEFCASE_EXPORTER", "otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    monkeypatch.setattr("sys.stdin", StringIO(""))
    question = "The original job inherited the default exporter from this shell with unchanged settings. " + runtime
    assert main(["record-help", question]) == 0
    assert "original launch configuration" in capsys.readouterr().out


@pytest.mark.parametrize("inline,expected", [
    ("BRIEFCASE_EXPORTER=otlp", "Inspect debug.err"),
    ("BRIEFCASE_EXPORTER=otlp OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/json", "Search the collector"),
    ("BRIEFCASE_EXPORTER=memory", "before exit"),
])
def test_confirmed_node_launch_overlays_inline_settings(monkeypatch, capsys, inline, expected):
    from io import StringIO

    from briefcase.cli.__main__ import main

    monkeypatch.setenv("BRIEFCASE_EXPORTER", "console")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", raising=False)
    monkeypatch.setattr("sys.stdin", StringIO(""))
    question = ("The Node preload inherited the unchanged settings from this shell. "
                + inline + " node --import @briefcase-ai/auto/register app.js 2> debug.err")
    assert main(["record-help", question]) == 0
    assert expected in capsys.readouterr().out


@pytest.mark.parametrize("raw_traces,expected", [("   ", "Search the collector"), ("", "Inspect debug.err")])
def test_node_selects_raw_protocol_before_trimming(raw_traces, expected, monkeypatch, capsys):
    from io import StringIO

    from briefcase.cli.__main__ import main

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", raw_traces)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    monkeypatch.setattr("sys.stdin", StringIO(""))
    question = ("The Node preload inherited the unchanged settings from this shell. "
                "BRIEFCASE_EXPORTER=otlp node --import @briefcase-ai/auto/register app.js 2> debug.err")
    assert main(["record-help", question]) == 0
    assert expected in capsys.readouterr().out
