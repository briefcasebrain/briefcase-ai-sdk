"""Harness unit tests. Tools are fake shell scripts on PATH; nothing is installed or fetched."""

import json
import os
import stat
import urllib.request

import pytest

from scripts.conformance import core
from scripts.conformance.capture import ReceiverProcess, Tee
from scripts.conformance.drivers import DRIVERS, CopilotCLI, GeminiCLI, first_version

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "receiver", "fixtures", "real")


@pytest.fixture
def fakebin(tmp_path, monkeypatch):
    directory = tmp_path / "fakebin"
    directory.mkdir()
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ["PATH"])

    def make(name, script):
        path = directory / name
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    return make


GEMINI_OK = '''
case "$1" in
  --version) echo 0.99.1;;
  mcp) echo "No MCP servers configured.";;
esac
'''

GEMINI_REJECTS = '''
case "$1" in
  --version) echo 0.99.1;;
  mcp) echo "Error in $PWD/.gemini/settings.json: nope"
       echo "Please fix the configuration file(s) and try again."; exit 1;;
esac
'''


def test_first_version():
    assert first_version("codex-cli 0.171.0\nWARNING x") == "0.171.0"
    assert first_version("2.1.330 (Claude Code)") == "2.1.330"
    assert first_version("nothing") is None


def test_levels_are_ordered():
    assert core.LEVELS == ("docs-only", "config-accepted", "telemetry-verified")
    assert core.level_rank("config-accepted") > core.level_rank("docs-only")
    assert core.level_rank("bogus") == -1


def test_driver_reaches_config_accepted_with_a_tool_that_accepts_the_file(fakebin):
    fakebin("gemini", GEMINI_OK)
    result = GeminiCLI(existing_binary=True).conform()
    assert result.status == "ok", [c.as_dict() for c in result.checks]
    assert result.level == core.CONFIG_ACCEPTED
    assert result.version == "0.99.1"
    assert "docs-only" not in result.level
    names = [c.name for c in result.checks]
    assert ".gemini/settings.json has the telemetry block" in names
    assert all(c.ok for c in result.checks)


def test_driver_fails_when_the_tool_rejects_the_file(fakebin):
    fakebin("gemini", GEMINI_REJECTS)
    result = GeminiCLI(existing_binary=True).conform()
    assert result.status == "failed"
    assert result.level == core.DOCS_ONLY
    assert any(not c.ok for c in result.checks)


def test_driver_fails_when_the_binary_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    result = GeminiCLI(existing_binary=True).conform()
    assert result.status == "failed"


def test_print_only_tool_never_claims_config_accepted(fakebin):
    fakebin("copilot", '''
case "$1" in
  --version) echo "GitHub Copilot CLI 9.9.9.";;
  help) echo "COPILOT_OTEL_ENABLED OTEL_EXPORTER_OTLP_ENDPOINT";;
  mcp) printf 'briefcase\\n  Status: Enabled\\n';;
esac
''')
    result = CopilotCLI(existing_binary=True).conform()
    assert result.status == "ok"
    assert result.level == core.DOCS_ONLY
    assert result.version == "9.9.9"


def test_desktop_apps_are_skipped_at_docs_only():
    for tool in ("vscode-copilot", "cursor"):
        result = DRIVERS[tool]().conform()
        assert result.status == "skipped" and result.level == core.DOCS_ONLY
        assert result.note


def test_every_installable_driver_names_its_docs_page_and_date():
    for name, cls in DRIVERS.items():
        if cls.package:
            assert cls.install_url.startswith("https://"), name
            assert cls.install_verified_on, name


def test_merge_keeps_last_known_level_when_a_run_fails():
    report = core.new_report()
    good = {"tool": "codex", "level": "config-accepted", "version": "0.1.0", "date": "2026-01-01", "status": "ok",
            "note": "", "install": "x", "checks": []}
    core.merge_results(report, [good])
    old = json.loads(json.dumps(report))
    bad = dict(good, status="failed", note="install failed", level="docs-only", version=None)
    core.merge_results(report, [bad])
    assert report["tools"]["codex"]["level"] == "config-accepted"
    assert report["tools"]["codex"]["last_attempt"]["status"] == "failed"
    assert any("run failed" in line for line in core.level_changes(old, report))


def test_level_changes_flags_a_downgrade_and_an_upgrade():
    old = {"tools": {"a": {"level": "telemetry-verified"}, "b": {"level": "docs-only"}, "c": {"level": "docs-only"}}}
    new = {"tools": {"a": {"level": "config-accepted"}, "b": {"level": "config-accepted"}, "c": {"level": "docs-only"}}}
    lines = core.level_changes(old, new)
    assert "a: DOWNGRADED telemetry-verified -> config-accepted" in lines
    assert "b: upgraded docs-only -> config-accepted" in lines
    assert not any(line.startswith("c:") for line in lines)


def test_clean_env_drops_ambient_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_USER_EMAIL", "someone@example.com")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    env = core.clean_env(tmp_path)
    assert "CLAUDE_CODE_USER_EMAIL" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["HOME"] == str(tmp_path)


def test_tee_forwards_real_payloads_to_a_real_receiver_and_records_appear():
    """The corpus capture is posted through the Tee into `briefcase receive`: same path the harness uses."""
    source = os.path.join(FIXTURES, "claude-code")
    versions = [d for d in os.listdir(source)] if os.path.isdir(source) else []
    if not versions:
        pytest.skip("no captured corpus yet")
    path = os.path.join(source, versions[0], "00-logs.json")
    with core.Sandbox() as sandbox, ReceiverProcess(sandbox, sandbox.env()) as receiver, \
            Tee(receiver.url, {"ok": True}) as tee:
        request = urllib.request.Request(tee.url + "/v1/logs", data=open(path, "rb").read(), method="POST",
                                         headers={"Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        assert opener.open(request, timeout=10).status == 200
        assert core.wait_for(lambda: bool(receiver.records()), 15)
        assert len(tee.captured) == 1
        model = urllib.request.Request(tee.url + "/v1/messages", data=b"{}", method="POST")
        assert json.loads(opener.open(model, timeout=10).read()) == {"ok": True}
        assert tee.model_requests == 1
