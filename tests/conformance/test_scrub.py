"""The scrubber must leave no e-mail, host path, token or identifier, and the committed corpus must be clean."""

import json
from pathlib import Path

import pytest

from scripts.conformance import scrub

REAL = Path(__file__).parent.parent / "receiver" / "fixtures" / "real"

DIRTY = {
    "text": "mail dev.person@example.com from /Users/dev.person/secret-project and C:\\Users\\dev\\proj "
            "key sk-ant-api03-abcdefghijklmnop ghp_abcdefghijklmnopqrstuvwx Bearer abcdefghijklmnop12345 "
            "session 5b1c2f9e-0000-4000-8000-000000000001 host 10.20.30.40 loopback 127.0.0.1:4318",
    "resourceLogs": [{"resource": {"attributes": [
        {"key": "user.email", "value": {"stringValue": "someone@corp.example"}},
        {"key": "user.account_uuid", "value": {"stringValue": "acct-opaque"}},
        {"key": "workspace.host_paths", "value": {"arrayValue": {"values": [{"stringValue": "/home/me/repo"}]}}},
        {"key": "service.name", "value": {"stringValue": "claude-code"}},
    ]}, "scopeLogs": [{"logRecords": [{"traceId": "a" * 32, "spanId": "b" * 16, "body": {"stringValue": "x"}}]}]}],
}


def test_scrubber_leaves_nothing_sensitive():
    cleaned = scrub.scrub_json(DIRTY)
    blob = json.dumps(cleaned)
    for needle in ("dev.person", "secret-project", "someone@corp.example", "acct-opaque", "/home/me", "sk-ant-",
                   "ghp_", "abcdefghijklmnop", "5b1c2f9e", "10.20.30.40", "C:\\\\Users"):
        assert needle not in blob, needle
    assert scrub.findings(blob) == []
    assert "127.0.0.1:4318" in blob            # loopback is not an identity
    assert "claude-code" in blob               # ordinary values survive


def test_scrubber_is_stable_so_references_still_line_up():
    one = "5b1c2f9e-0000-4000-8000-000000000001"
    cleaned = scrub.Scrubber().obj({"a": one, "b": [one], "c": "a@b.co and a@b.co"})
    assert cleaned["a"] == cleaned["b"][0] != one
    assert cleaned["c"].count("user1@example.invalid") == 2


def test_trace_ids_stay_linked_but_are_not_real():
    cleaned = scrub.scrub_json({"traceId": "a" * 32, "x": {"traceId": "a" * 32, "spanId": "b" * 16}})
    assert cleaned["traceId"] == cleaned["x"]["traceId"] != "a" * 32
    assert len(cleaned["x"]["spanId"]) == 16


def test_scrub_json_refuses_to_return_something_it_could_not_clean(monkeypatch):
    monkeypatch.setattr(scrub.Scrubber, "text", lambda self, value: value)   # a broken scrubber
    with pytest.raises(ValueError):
        scrub.scrub_json({"a": "leak@example.com"})


def test_findings_detects_each_kind():
    text = "a@b.com /home/x/y 5b1c2f9e-0000-4000-8000-000000000001 sk-abcdefghij 8.8.8.8"
    kinds = {k for k, _ in scrub.findings(text)}
    assert kinds >= {"email", "unix-path", "uuid", "token", "ip"}


CORPUS = sorted(REAL.rglob("*.json")) or [pytest.param(None, marks=pytest.mark.skip("empty corpus"))]


@pytest.mark.parametrize("path", CORPUS)
def test_committed_corpus_is_clean(path):
    assert scrub.findings(path.read_text(encoding="utf-8")) == []
