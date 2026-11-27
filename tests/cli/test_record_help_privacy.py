"""A pasted launch command may contain credentials alongside a destination."""
from io import StringIO

import pytest


@pytest.mark.parametrize("credential", [
    "BRIEFCASE_EXPORTER_TOKEN=demo-712",
    "Authorization: Bearer demo-712",
    "OTEL_EXPORTER_OTLP_HEADERS='authorization=Bearer demo-712'",
    'API_KEY="demo-712 secondary-906"',
    "access_token: demo-712",
    "--password demo-712",
    "--api-token demo-712",
    "--provider-auth-token=demo-712",
    "--clientSecret demo-712",
    "--http-authorization-header demo-712",
    "My API token is demo-712",
    "My session token is demo-712",
    "My personal token is demo-712",
    "My CI job token is demo-712",
    "This is the proxy trace token demo-712",
    "My delegated job token is demo-712",
    "The authentication ticket was demo-712",
    "Ticket demo-712 was unique to the original job",
    "Our original token was demo-712",
    "The bearer credential we used was demo-712",
    "The HTTP password in the launch configuration was demo-712",
    "My provider API key is demo-712",
])
def test_credentials_are_never_used_as_correlation(credential, monkeypatch, capsys):
    from briefcase.cli.__main__ import main

    evidence = f"{credential} My original exporter used HTTPS https://example.invalid/archive.jsonl"
    monkeypatch.setattr("sys.stdin", StringIO(evidence + "\n"))
    assert main(["record-help"]) == 0
    captured = capsys.readouterr()
    result = captured.out + captured.err
    assert "receiver" in result.lower()
    assert not any(value in result for value in ("demo-712", "secondary-906", "example.invalid"))


@pytest.mark.parametrize("reverse", [False, True])
def test_private_input_preserves_original_process_scope(reverse, monkeypatch, capsys):
    from briefcase.cli.__main__ import main

    clauses = ["My original run used the console exporter", "Another process used OTLP with an API token"]
    if reverse:
        clauses.reverse()
    monkeypatch.setattr("sys.stdin", StringIO(". ".join(clauses) + "\n"))
    assert main(["record-help"]) == 0
    result = capsys.readouterr().out
    assert "stderr" in result and "collector" not in result


@pytest.mark.parametrize("observation,expected", [
    ("A second process opened events.jsonl and found a record containing receipt-718.test.", "reported readback"),
    ("A second process opened events.jsonl. Only yesterday's token was present.", "found no row"),
    ("A second process opened events.jsonl and found a record containing receipt-718.test2.", "search locally"),
    ("A second process opened events.jsonl and found a record containing private invocation marker.", "search locally"),
    ("No reader has reopened the file.", "search locally"),
    ("A reader opened other.jsonl and found a row containing receipt-718.test.", "search locally"),
    ("A reader opened events.jsonl. I deny that it found a row containing receipt-718.test.", "search locally"),
])
def test_unique_job_tokens_stay_private_during_file_matching(observation, expected, monkeypatch, capsys):
    from briefcase.cli.__main__ import main

    question = "Explicit JSONL to events.jsonl. Job token receipt-718.test was unique to this invocation. "
    monkeypatch.setattr("sys.stdin", StringIO(question + observation + "\n"))
    assert main(["record-help"]) == 0
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "events.jsonl" in text and "job token from your original invocation" in text
    assert expected in text and "receipt-718" not in text


def test_private_marker_cannot_return_as_clarified_id(monkeypatch, capsys):
    from briefcase.cli.__main__ import main

    question = "I have two identical tickets. Job token repeat-321 was unique to this invocation."
    monkeypatch.setattr("sys.stdin", StringIO(question + "\nrepeat-321\n"))
    assert main(["record-help"]) == 0
    captured = capsys.readouterr()
    assert "static guide" in captured.out
    assert "repeat-321" not in captured.out + captured.err


@pytest.mark.parametrize("credential", [
    "My CI job token is", "My workflow credential was", "The delegated job token was",
])
@pytest.mark.parametrize("question", [
    "My original HTTP exporter used https://example.invalid/logs. {label} demo-712. My original marker was demo-712.",
    "I have two identical tickets. {label} demo-712.\ndemo-712",
    "{label} demo-712. The original marker was demo-712. The explicit local export was to demo-712.jsonl.",
])
def test_credential_relabelled_as_marker_or_file_remains_private(credential, question, monkeypatch, capsys):
    from briefcase.cli.__main__ import main

    monkeypatch.setattr("sys.stdin", StringIO(question.format(label=credential) + "\n"))
    assert main(["record-help"]) == 0
    captured = capsys.readouterr()
    assert "demo-712" not in captured.out + captured.err
