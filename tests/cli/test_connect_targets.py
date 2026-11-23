"""`connect` never modifies an existing target in place. For each target (tool, kind, scope, file or registry,
profile): absent and not journalled -> written; journalled and still exactly what we wrote -> "already
connected" (settings that differ -> nothing changes, non-zero, say to undo and connect again); journalled but
changed or gone -> not ours any more, untouched and never printed; present but not journalled -> untouched."""

import json
import os
import sys

import pytest

from briefcase.cli.__main__ import main
from briefcase.connect import engine

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX paths")


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for directory in (home, tmp_path / "project", tmp_path / "bin"):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.delenv("BRIEFCASE_HOME", raising=False)
    monkeypatch.delenv("BRIEFCASE_RECEIVER_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.chdir(tmp_path / "project")
    from briefcase.receiver import daemon
    monkeypatch.setattr(daemon, "probe", lambda endpoint, timeout=1.0: None)
    return home


def fake_bin(tmp_path, name, output="1.0.0"):
    path = tmp_path / "bin" / name
    path.write_text(f"#!/bin/sh\necho '{output}'\n")
    os.chmod(path, 0o755)


def claude_env(home):
    return json.loads((home / ".claude" / "settings.json").read_text())["env"]


REMOTE = ["--endpoint", "https://claude.example.com", "--headers", "Authorization=Bearer SECRET-1",
          "--capture-content"]


def snapshot(home):
    return {str(p.relative_to(home)): p.read_text() for p in sorted(home.rglob("*"))
            if p.is_file() and ".briefcase" not in p.parts}


def test_a_second_bare_connect_says_already_connected_and_changes_nothing(home, capsys):
    assert main(["connect", "claude-code", "--no-mcp", "--yes", *REMOTE]) == 0
    before = snapshot(home)
    capsys.readouterr()
    assert main(["connect", "claude-code", "--no-mcp", "--yes"]) == 0
    assert "already connected" in capsys.readouterr().out
    assert snapshot(home) == before


def test_the_content_warning_prints_when_a_flag_writes_the_target_and_not_for_an_untouched_one(home, capsys):
    assert main(["connect", "claude-code", "--no-mcp", "--yes", "--capture-content"]) == 0
    assert "prompt" in capsys.readouterr().err.lower()
    assert main(["connect", "claude-code", "--no-mcp", "--yes"]) == 0
    assert capsys.readouterr().err == ""  # nothing was written, so nothing to warn about


def test_a_new_scope_after_a_user_scope_remote_connect_changes_nothing_and_says_to_undo(home, capsys):
    assert main(["connect", "claude-code", "--no-mcp", "--yes", *REMOTE]) == 0
    before = snapshot(home)
    capsys.readouterr()
    assert main(["connect", "claude-code", "--no-mcp", "--yes", "--scope", "project"]) == 1
    out = capsys.readouterr().out
    assert snapshot(home) == before and "connect --undo claude-code" in out
    assert claude_env(home)["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://claude.example.com"


def test_force_on_an_out_of_range_version_does_not_redirect_an_existing_target(home, tmp_path, capsys):
    assert main(["connect", "claude-code", "--no-mcp", "--yes", *REMOTE]) == 0
    fake_bin(tmp_path, "claude", "0.0.1 (Claude Code)")
    before = snapshot(home)
    assert main(["connect", "claude-code", "--no-mcp", "--yes", "--force"]) in (0, 1)
    assert snapshot(home) == before


def test_a_new_endpoint_after_local_capture_changes_nothing_and_does_not_carry_capture_over(
        home, tmp_path, capsys):
    assert main(["connect", "cursor", "--no-mcp", "--yes", "--capture-content"]) == 0
    hooks = home / ".cursor" / "hooks.json"
    before = hooks.read_text()
    assert main(["connect", "cursor", "--no-mcp", "--yes", "--endpoint", "https://new.example.com"]) == 1
    assert hooks.read_text() == before and "new.example.com" not in hooks.read_text()
    assert "connect --undo cursor" in capsys.readouterr().out


def test_codex_scope_switch_keeps_the_header(home, capsys):
    assert main(["connect", "codex", "--no-mcp", "--yes", "--endpoint", "https://codex.example.com",
                 "--headers", "Authorization=Bearer SECRET-2"]) == 0
    config = home / ".codex" / "config.toml"
    before = config.read_text()
    assert main(["connect", "codex", "--no-mcp", "--yes", "--scope", "project"]) == 1
    assert config.read_text() == before and "SECRET-2" in before


def test_codex_with_only_the_mcp_table_removed_is_modified_since_connect_and_left_alone(home, capsys):
    assert main(["connect", "codex", "--yes", "--endpoint", "https://codex.example.com",
                 "--headers", "Authorization=Bearer SECRET-3"]) == 0
    config = home / ".codex" / "config.toml"
    text = config.read_text()
    start = text.index("[mcp_servers.briefcase]")
    end = text.index("# <<< briefcase <<<")
    config.write_text(text[:start] + text[end:])
    edited = config.read_text()
    capsys.readouterr()
    assert main(["connect", "codex", "--no-mcp", "--yes"]) == 0  # a skipped step, not a failure
    out = capsys.readouterr().out
    assert config.read_text() == edited and "modified since connect" in out and "SECRET-3" not in out
    assert main(["mcp", "install", "codex", "--yes"]) == 1


def test_a_formerly_owned_project_entry_the_user_replaced_is_not_ours_any_more(
        home, tmp_path, capsys, monkeypatch):
    mcp_json = tmp_path / "project" / ".mcp.json"
    assert main(["mcp", "install", "claude-code", "--scope", "project", "--yes"]) == 0
    entry = {"command": "mine", "args": ["--token=SECRET-4"]}
    mcp_json.write_text(json.dumps({"mcpServers": {"briefcase": entry}}))
    capsys.readouterr()
    assert main(["connect", "claude-code", "--scope", "project", "--yes"]) == 0
    captured = capsys.readouterr()
    assert json.loads(mcp_json.read_text())["mcpServers"]["briefcase"] == entry
    assert "SECRET-4" not in captured.out + captured.err and "modified since connect" in captured.out
    assert main(["mcp", "install", "claude-code", "--scope", "project", "--yes"]) == 1
    # Switching the Claude profile does not make the replaced entry ours again.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "other-profile"))
    assert main(["connect", "claude-code", "--scope", "project", "--yes"]) == 0
    assert json.loads(mcp_json.read_text())["mcpServers"]["briefcase"] == entry


def test_other_entries_the_user_adds_to_a_connected_file_do_not_make_it_modified(home, tmp_path, capsys):
    mcp_json = tmp_path / "project" / ".mcp.json"
    assert main(["connect", "claude-code", "--scope", "project", "--yes", "--no-mcp"]) == 0
    assert main(["mcp", "install", "claude-code", "--scope", "project", "--yes"]) == 0
    doc = json.loads(mcp_json.read_text())
    doc["mcpServers"]["mine"] = {"command": "x"}
    mcp_json.write_text(json.dumps(doc))
    edited = mcp_json.read_text()
    capsys.readouterr()
    assert main(["mcp", "install", "claude-code", "--scope", "project", "--yes"]) == 0
    assert "already connected" in capsys.readouterr().out and mcp_json.read_text() == edited


def test_an_identical_unjournalled_project_entry_also_prints_the_instruction(home, tmp_path, capsys):
    from briefcase.connect import mcp as payloads

    mcp_json = tmp_path / "project" / ".mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {"briefcase": payloads.server_config()}}))
    capsys.readouterr()
    assert main(["connect", "claude-code", "--scope", "project", "--yes"]) == 0
    assert "did not write" in capsys.readouterr().out


def _old_journal(home, tmp_path):
    journal = home / ".briefcase" / "connect" / "journal.json"
    journal.parent.mkdir(parents=True)
    old = home / ".codex" / "config.toml"
    old.parent.mkdir()
    old.write_text("# >>> briefcase >>>\n[otel]\n# <<< briefcase <<<\n")
    add = ["claude", "mcp", "add", "--scope", "user", "briefcase", "--", "old"]
    remove = ["claude", "mcp", "remove", "--scope", "user", "briefcase"]
    journal.write_text(json.dumps({
        "version": 1, "files": {str(old): {"format": "toml", "ops": [{"op": "toml_block", "tool": "codex"}]}},
        "commands": [
            {"tool": "claude-code", "argv": add, "undo_hint": remove, "config_dir": "/p/a", "cwd": None},
            {"tool": "claude-code", "argv": add, "undo_hint": remove, "config_dir": "/p/b", "cwd": None}],
        "tools": {}}))
    return journal, old


def test_an_older_journal_changes_nothing_and_lists_what_the_earlier_build_recorded(
        home, tmp_path, capsys, monkeypatch):
    journal, old = _old_journal(home, tmp_path)
    calls = []
    monkeypatch.setattr(engine, "run_command", lambda argv: calls.append(argv) or (0, ""))
    before, raw = old.read_text(), journal.read_text()
    assert main(["connect", "codex", "--yes"]) == 1
    out = capsys.readouterr()
    text = out.out + out.err
    assert str(old) in text and str(journal) in text and "delete" in text.lower()
    assert text.count("claude mcp remove --scope user briefcase") == 2  # both profiles are listed
    assert old.read_text() == before and journal.read_text() == raw and not calls
    assert main(["connect", "--undo"]) == 1
    assert main(["mcp", "install", "claude-code", "--yes"]) == 1
    assert journal.read_text() == raw and old.read_text() == before and not calls


def test_an_older_journal_never_owns_the_block_it_lists(home, tmp_path, capsys):
    journal, old = _old_journal(home, tmp_path)
    journal.unlink()  # the user deleted it as told, but left the block the earlier build wrote
    capsys.readouterr()
    assert main(["connect", "codex", "--no-mcp", "--yes"]) == 0
    assert old.read_text() == "# >>> briefcase >>>\n[otel]\n# <<< briefcase <<<\n"
    assert "did not write" in capsys.readouterr().out


def test_undo_removes_only_what_still_matches_and_reports_the_rest(home, capsys):
    assert main(["connect", "codex", "--no-mcp", "--yes", "--endpoint", "https://codex.example.com"]) == 0
    config = home / ".codex" / "config.toml"
    config.write_text(config.read_text().replace("codex.example.com", "mine.example.com"))
    edited = config.read_text()
    capsys.readouterr()
    assert main(["connect", "--undo", "codex"]) == 0
    assert config.read_text() == edited and "changed after connect" in capsys.readouterr().out
    # Claude: one key we wrote is changed by the user, the rest are removed.
    assert main(["connect", "claude-code", "--no-mcp", "--yes", *REMOTE]) == 0
    settings = home / ".claude" / "settings.json"
    doc = json.loads(settings.read_text())
    doc["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://mine.example.com"
    settings.write_text(json.dumps(doc))
    capsys.readouterr()
    assert main(["connect", "--undo", "claude-code"]) == 0
    assert json.loads(settings.read_text()) == {"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://mine.example.com"}}
    assert "changed after connect" in capsys.readouterr().out
