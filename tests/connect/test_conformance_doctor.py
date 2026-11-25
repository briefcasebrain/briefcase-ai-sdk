"""`briefcase-ai doctor --connect` reads the shipped conformance report and compares versions."""

import json
from argparse import Namespace

from briefcase.cli import doctor
from briefcase.connect import conformance

REPORT = {"tools": {
    "codex": {"level": "config-accepted", "version": "0.171.0", "date": "2026-11-20"},
    "claude-code": {"level": "telemetry-verified", "version": "2.1.330", "date": "2026-11-21"},
    "cursor": {"level": "docs-only", "version": None, "date": "2026-11-21"},
}}


def test_newer_version_is_flagged_untested():
    sev, detail = conformance.describe("Codex CLI", "0.180.2", REPORT["tools"]["codex"])
    assert sev == "warn"
    assert detail == "Codex CLI 0.171.0: config-accepted on 2026-11-20; your version 0.180.2 is newer, untested"


def test_same_older_and_unknown_versions():
    entry = REPORT["tools"]["codex"]
    assert conformance.describe("Codex CLI", "0.171.0", entry)[0] == "ok"
    sev, detail = conformance.describe("Codex CLI", "0.150.0", entry)
    assert sev == "ok" and "older" in detail
    assert conformance.describe("Codex CLI", None, entry)[0] == "warn"


def test_docs_only_and_missing_entries_do_not_claim_a_tested_version():
    sev, detail = conformance.describe("Cursor", "1.0.0", REPORT["tools"]["cursor"])
    assert sev == "ok" and "never run against a real install" in detail
    assert "no conformance data" in conformance.describe("X", "1.0", None)[1]


def test_missing_or_broken_report_is_an_empty_report(tmp_path):
    assert conformance.load_report(tmp_path / "nope.json") == {"tools": {}}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert conformance.load_report(bad) == {"tools": {}}


def test_shipped_report_is_valid():
    report = conformance.load_report()
    assert report["tools"], "conformance-report.json must ship with results"
    for tool, entry in report["tools"].items():
        assert entry["level"] in conformance.LEVELS, tool


def test_doctor_connect_appends_conformance_rows_and_keeps_existing_output(home, fake_bin, monkeypatch, capsys):
    fake_bin("codex", "codex-cli 0.180.2")
    path = home.parent / "report.json"
    path.write_text(json.dumps(REPORT))
    monkeypatch.setattr(conformance, "REPORT_FILE", path)
    rc = doctor.cmd_doctor_connect(Namespace(strict=False))
    out = capsys.readouterr().out
    assert rc == 0                                           # a newer version is a warning, not a failure
    assert out.startswith("briefcase-ai connect integrations:")
    assert "[warn] connect: no tools connected yet; run: briefcase-ai connect" in out      # existing output intact
    assert ("[warn] conformance codex: Codex CLI 0.171.0: config-accepted on 2026-11-20; "
            "your version 0.180.2 is newer, untested") in out


def test_strict_turns_the_untested_warning_into_a_failure(home, fake_bin, monkeypatch, tmp_path):
    fake_bin("codex", "codex-cli 0.180.2")
    path = tmp_path / "report.json"
    path.write_text(json.dumps(REPORT))
    monkeypatch.setattr(conformance, "REPORT_FILE", path)
    assert doctor.cmd_doctor_connect(Namespace(strict=True)) == 1
