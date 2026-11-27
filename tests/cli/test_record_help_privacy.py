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
])
def test_credentials_are_never_used_as_correlation(credential, monkeypatch, capsys):
    from briefcase.cli.__main__ import main

    evidence = f"{credential} My original exporter used HTTPS https://example.invalid/archive.jsonl"
    monkeypatch.setattr("sys.stdin", StringIO(evidence + "\n"))
    assert main(["record-help"]) == 0
    result = capsys.readouterr().out
    assert "receiver" in result.lower()
    assert not any(value in result for value in ("demo-712", "secondary-906", "example.invalid"))
