"""Each case the round-8 review reproduced, named, next to the generated invariants."""
import json
import os
import sys

import pytest

from briefcase.connect import edits, mcpreg

from tests.connect.test_invariants import SECRET, SUPPLIED, World

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX paths and modes")


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def test_a_comment_the_user_adds_inside_our_markers_survives_undo(w):
    assert w.run(["connect", "codex", "--yes", "--no-mcp"])[0] == 0
    path = w.telemetry_file("codex")
    text = path.read_text().replace("log_user_prompt", "# maintenance: do not change\nlog_user_prompt", 1)
    path.write_text(text)
    w.run(["connect", "--undo", "codex"])
    assert "# maintenance: do not change" in path.read_text()


def test_undo_refuses_a_document_that_gained_a_duplicate_key_after_install(w):
    assert w.run(["connect", "claude-code", "--yes", "--no-mcp"])[0] == 0
    path = w.telemetry_file("claude-code")
    path.write_text(path.read_text().rstrip().rstrip("}") + ',\n  "env": {"X": "1"}\n}\n')
    before = path.read_bytes()
    code, out, err = w.run(["connect", "--undo", "claude-code"])
    assert path.read_bytes() == before and "unambiguously" in out + err


def test_credentials_the_redactor_was_never_told_about_are_masked_by_shape():
    text = edits.redact("boom: Authorization=Bearer ZZZZZZZZ and token: abcd1234 and x-api-key=QQQQQQ", [])
    assert "ZZZZZZZZ" not in text and "abcd1234" not in text and "QQQQQQ" not in text


def test_a_malformed_header_and_the_retry_hint_do_not_echo_the_argument(w):
    code, out, err = w.run(["connect", "claude-code", "--yes", "--headers", "Bearer " + SUPPLIED])
    assert code == 2 and SUPPLIED not in out + err
    from briefcase.cli.__main__ import _scrub_argv

    assert SUPPLIED not in " ".join(_scrub_argv(["connect", "--headers", "A=" + SUPPLIED, "--token=" + SUPPLIED]))


def test_claude_boundaries_come_from_its_own_config_when_it_has_the_entry(w, tmp_path):
    add = ["claude", "mcp", "add", "--scope", "user", "briefcase", "--", "uvx", "--from", "a", "b"]
    head = ("briefcase:\n  Scope: User config (available in all your projects)\n  Type: stdio\n  Command: uvx\n"
            "  Args: --from a b\n  Environment:\n")
    same_text = lambda argv: (0, head)  # noqa: E731
    (w.home / ".claude.json").write_text(json.dumps({"mcpServers": {"briefcase": {
        "command": "uvx", "args": ["--from", "a", "b"]}}}))
    assert mcpreg.owned(add, same_text) == mcpreg.OURS
    (w.home / ".claude.json").write_text(json.dumps({"mcpServers": {"briefcase": {
        "command": "uvx", "args": ["--from", "a b"]}}}))  # the flattened line reads the same, the boundaries do not
    assert mcpreg.owned(add, same_text) == mcpreg.DIFFERS
    (w.home / ".claude.json").write_text(json.dumps({"mcpServers": {"other": {}}}))
    assert mcpreg.owned(add, same_text) == mcpreg.UNKNOWN


def test_vscode_follows_vscode_appdata_and_its_journal_identity_is_not_the_claude_variable(w, monkeypatch, tmp_path):
    monkeypatch.setenv("VSCODE_APPDATA", str(tmp_path / "appdata"))
    assert w.vscode_settings() == tmp_path / "appdata" / "Code" / "User" / "settings.json"
    from briefcase.connect import engine

    argv = ["code", "--add-mcp", "{}"]
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    assert engine.identity(argv)["profile"] == str(tmp_path / "appdata")


def test_policy_off_does_not_say_nothing_is_installed_while_an_unrecorded_hook_enforces(w):
    assert w.run(["policy", "init", "claude-code", "--yes"])[0] == 0
    edits.journal_path().unlink()  # the record is lost; the hook is still on disk
    code, out, err = w.run(["policy", "off", "claude-code", "--yes"])
    assert code == 1 and "Enforcement is still ON" in out + err and "No policy hooks are installed" not in out


@pytest.mark.parametrize("where, key", [
    ("user", "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"), ("project_local", "OTEL_LOG_RAW_API_BODIES"),
    ("project", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"),
])
def test_telemetry_settings_in_any_layer_block_a_first_connect(w, where, key):
    if where == "user":
        path = w.telemetry_file("claude-code")
    else:
        path = w.project / ".claude" / ("settings.local.json" if where == "project_local" else "settings.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": {key: "1"}}))
    code, out, err = w.run(["connect", "claude-code", "--no-mcp", "--yes", "--endpoint", "https://new.example.com"])
    assert code == 1 and key in out + err
    assert not (w.telemetry_file("claude-code").exists() and "new.example.com" in w.telemetry_file(
        "claude-code").read_text())


def test_an_interrupted_connect_still_records_what_it_already_wrote(w, monkeypatch):
    real = edits.apply
    calls = []

    def second_write_dies(resolved, journal):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(resolved, journal)

    monkeypatch.setattr(edits, "apply", second_write_dies)
    assert w.run(["connect", "cursor", "--yes"])[0] == 130
    assert [t["kind"] for t in edits.load_journal()["targets"].values()] == ["telemetry"], \
        "a hook was written and no record names it"


def test_a_pack_that_stops_halfway_keeps_the_old_ownership_marker(w, tmp_path, monkeypatch):
    from argparse import Namespace

    from briefcase.policy import managed

    out_dir = tmp_path / "pack"

    def args(**kw):
        values = dict(out=str(out_dir), endpoint="https://otel.example.com", policy=None, managed_tools=None,
                      tools=[], headers=[], capture_content=False, force=False, connect_dry_run=False,
                      dry_run=False)
        values.update(kw)
        return Namespace(**values)

    assert managed.cmd_managed(args(), out=lambda t: None) == 0
    marker = (out_dir / managed.PACK_MARK).read_text()
    real = edits.write_checked

    def dies_on_codex(target, text, expect, mode):
        if "codex" in str(target):
            raise edits.EditRefused("simulated")
        return real(target, text, expect, mode)

    monkeypatch.setattr(edits, "write_checked", dies_on_codex)
    managed.cmd_managed(args(force=True, endpoint="https://other.example.com"), out=lambda t: None)
    assert (out_dir / managed.PACK_MARK).read_text() == marker, "the marker moved before the pack was complete"
    assert os.path.exists(out_dir / "manifest.json") and SECRET not in marker
