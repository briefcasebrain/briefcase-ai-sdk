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
