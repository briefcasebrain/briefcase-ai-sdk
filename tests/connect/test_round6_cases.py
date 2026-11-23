"""Each case the round-6 review reproduced, named, next to the generated invariants in test_invariants.py."""
import json
import os
import sys

import pytest

from briefcase.connect import edits

from tests.connect.test_invariants import JSON_SEEDS, SECRET, World, seed

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX paths and modes")


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


@pytest.mark.parametrize("tool, state, key", [
    ("claude-code", "capture_on", "OTEL_LOG_USER_PROMPTS"),
    ("claude-code", "headers_on", "OTEL_EXPORTER_OTLP_HEADERS"),
    ("vscode-copilot", "capture_on", "captureContent"),
])
def test_first_connect_does_not_send_existing_capture_or_credentials_to_a_new_endpoint(w, tool, state, key):
    seed(w, tool, state)
    path = w.telemetry_file(tool)
    before = path.read_bytes()
    code, out, err = w.run(["connect", tool, "--no-mcp", "--yes", "--endpoint", "https://new.example.com"])
    text = out + err
    assert code == 1 and path.read_bytes() == before
    assert key in text and SECRET not in text and "Nothing was written" in out
    # Saying so explicitly is allowed: the capture or credential is then asked for, not inherited.
    flag = ["--capture-content"] if state == "capture_on" else ["--headers", "Authorization=Bearer x"]
    again, _, _ = w.run(["connect", tool, "--no-mcp", "--yes", "--endpoint", "https://new.example.com", *flag])
    # The key is the user's own and is never overwritten, so nothing is written either way.
    assert again in (0, 1) and path.read_bytes() == before


def test_a_preview_shows_only_the_lines_it_would_add(w):
    (w.project / ".mcp.json").write_text(JSON_SEEDS["claude-code"]["compact_secret"])
    for argv in (["mcp", "install", "claude-code", "--scope", "project", "--dry-run"],
                 ["connect", "claude-code", "--scope", "project", "--dry-run", "--no-telemetry"][:5]):
        code, out, err = w.run(argv)
        assert SECRET not in out + err and '"o"' not in out and "API_KEY" not in out
    assert "+" in out
    w.telemetry_file("cursor").parent.mkdir(parents=True)
    w.telemetry_file("cursor").write_text(JSON_SEEDS["cursor"]["compact_secret"])
    code, out, err = w.run(["policy", "init", "cursor", "--dry-run"])
    assert SECRET not in out + err


def test_codex_add_never_replaces_an_unrecorded_same_named_entry(w):
    seed(w, "codex", "registry_foreign")
    path = w.telemetry_file("codex")
    before = path.read_bytes()
    code, out, err = w.run(["mcp", "install", "codex", "--yes"])
    adds = [c for c in w.cli.calls if c[:3] == ["codex", "mcp", "add"]]
    assert code == 1 and path.read_bytes() == before and not adds
    assert SECRET not in out + err


def test_a_recorded_entry_with_an_extra_argument_is_not_ours(w):
    assert w.run(["mcp", "install", "claude-code", "--yes"])[0] == 0
    key = (None, "user")
    w.cli.claude[key] = w.cli.claude[key] + ["--extra-secret=" + SECRET]  # substring matching would accept this
    code, out, err = w.run(["mcp", "install", "claude-code", "--yes"])
    assert code == 1 and "no longer treated as briefcase's" in out and SECRET not in out + err
    assert not [c for c in w.cli.calls if c[:3] == ["claude", "mcp", "add"]][1:]


def test_an_unavailable_registry_lookup_is_unknown_and_never_ours(w, monkeypatch):
    assert w.run(["mcp", "install", "claude-code", "--yes"])[0] == 0
    real = w.cli.__call__

    def broken(argv):
        return (1, "timed out") if argv[:3] == ["claude", "mcp", "get"] else real(argv)

    monkeypatch.setattr("briefcase.connect.engine.run_command", broken)
    code, out, _ = w.run(["mcp", "install", "claude-code", "--yes"])
    assert code == 1 and "could not be read" in out
    code, out, _ = w.run(["connect", "--undo", "claude-code"])
    assert "to remove the MCP registration" not in out and "could not be read" in out


def test_undo_rechecks_ownership_and_names_the_profile(w, tmp_path, monkeypatch):
    profile = str(tmp_path / "profile-a")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", profile)
    assert w.run(["mcp", "install", "claude-code", "--yes"])[0] == 0
    code, out, _ = w.run(["connect", "--undo", "claude-code", "--dry-run"])
    assert f"CLAUDE_CONFIG_DIR={profile} claude mcp remove --scope user briefcase" in out
    # Another profile: nothing to run, and the instruction says where to run it.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "profile-b"))
    code, out, _ = w.run(["connect", "--undo", "claude-code", "--dry-run"])
    assert "to remove the MCP registration" not in out
    assert f"CLAUDE_CONFIG_DIR={profile} briefcase-ai connect --undo" in out
    # The user replaced the entry in profile A: undo prints no removal at all.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", profile)
    w.cli.claude[(profile, "user")] = ["node", "mine.js"]
    code, out, _ = w.run(["connect", "--undo", "claude-code", "--dry-run"])
    assert "to remove the MCP registration" not in out and "no longer briefcase's" in out


def test_profile_variables_are_honoured_by_connect_and_by_policy_hooks(w, tmp_path, monkeypatch):
    profile, codex = tmp_path / "claude-profile", tmp_path / "codex-profile"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    assert w.run(["connect", "claude-code", "codex", "--no-mcp", "--yes"])[0] == 0
    assert (profile / "settings.json").exists() and (codex / "config.toml").exists()
    assert not (w.home / ".claude").exists() and not (w.home / ".codex").exists()
    assert w.run(["policy", "init", "claude-code", "codex", "--yes"])[0] == 0
    assert "hooks" in json.loads((profile / "settings.json").read_text())
    assert (codex / "hooks.json").exists() and not (w.home / ".claude").exists() and not (w.home / ".codex").exists()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative")
    code, out, err = w.run(["connect", "claude-code", "--no-mcp", "--yes"])
    assert code == 1 and "CLAUDE_CONFIG_DIR" in out + err


def test_a_localhost_endpoint_is_written_into_the_cursor_hook_whatever_the_environment_says(w):
    assert w.run(["connect", "cursor", "--no-mcp", "--yes", "--endpoint", "http://127.0.0.1:4318"])[0] == 0
    hooks = json.loads(w.telemetry_file("cursor").read_text())["hooks"]
    commands = [h["command"] for entries in hooks.values() for h in entries]
    assert commands and all("--endpoint http://127.0.0.1:4318" in c for c in commands)


def test_start_in_project_b_does_not_pick_project_a_s_recorded_remote_endpoint(w, tmp_path, monkeypatch):
    from briefcase.cli import receive_commands

    started = []
    monkeypatch.setattr(receive_commands, "start_detached", lambda args, out=print: started.append(args) or 0)
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    assert w.run(["connect", "cursor", "--no-mcp", "--scope", "project", "--yes", "--endpoint",
                  "https://remote.example.com"])[0] == 0
    monkeypatch.chdir(second)
    assert w.run(["connect", "cursor", "--no-mcp", "--scope", "project", "--yes", "--start"])[0] == 0
    assert len(started) == 1


def test_a_settings_conflict_writes_nothing_else_and_a_dry_run_says_so(w):
    assert w.run(["connect", "claude-code", "--no-mcp", "--yes", "--endpoint", "https://claude.example.com"])[0] == 0
    before = w.snapshot()
    code, out, _ = w.run(["connect", "claude-code", "--yes", "--scope", "project"])
    assert code == 1 and w.snapshot() == before and not (w.project / ".mcp.json").exists()
    assert w.run(["connect", "claude-code", "--yes", "--scope", "project", "--dry-run"])[0] == 1


def test_status_reports_a_modified_codex_block_and_a_missing_mcp_table(w):
    (w.bin / "codex").unlink()  # no codex on PATH: the MCP table goes into the file, not through the CLI
    assert w.run(["connect", "codex", "--yes", "--no-mcp"])[0] == 0
    path = w.telemetry_file("codex")
    path.write_text(path.read_text().replace("127.0.0.1:4318", "mine.example.com"))
    code, out, _ = w.run(["connect", "status"])
    assert "DRIFTED" in out and "MISSING" in out
    # Telemetry first, then a full connect: the missing table is not "already connected".
    assert w.run(["connect", "--undo", "codex"])[0] == 0
    path.write_text("")
    assert w.run(["connect", "codex", "--no-mcp", "--yes"])[0] == 0
    code, out, _ = w.run(["connect", "codex", "--yes"])
    assert code == 1 and "already connected" not in out and "connect --undo codex" in out


def test_policy_force_never_replaces_a_policy_the_user_wrote(w):
    policy = w.home / ".briefcase" / "policy.toml"
    policy.parent.mkdir(parents=True, exist_ok=True)
    mine = 'version = 1\npreset = "strict"\n\n[[rule]]\nid = "mine"\ntool = "shell"\neffect = "deny"\ncommand = "x*"\n'
    policy.write_text(mine)
    code, out, err = w.run(["policy", "init", "claude-code", "--yes", "--force"])
    assert code == 1 and policy.read_text() == mine and "will not replace it" in out + err
    assert not list(policy.parent.glob("policy.toml.briefcase-bak-*"))
    # A policy that `policy init` wrote may be replaced.
    policy.unlink()
    assert w.run(["policy", "init", "claude-code", "--yes"])[0] == 0
    assert w.run(["policy", "init", "claude-code", "--yes", "--force"])[0] == 0
    assert list(policy.parent.glob("policy.toml.briefcase-bak-*"))  # replaced, with a backup


def test_a_deleted_hook_is_not_reported_as_already_installed(w):
    assert w.run(["policy", "init", "claude-code", "--yes"])[0] == 0
    path = w.telemetry_file("claude-code")
    path.write_text("{}")
    code, out, err = w.run(["policy", "init", "claude-code", "--yes"])
    assert code == 1 and "already installed" not in out and "NOT in place" in out


def test_an_undo_after_a_json_true_became_one_leaves_the_changed_value(w):
    assert w.run(["connect", "vscode-copilot", "--no-mcp", "--yes"])[0] == 0
    path = w.telemetry_file("vscode-copilot")
    path.write_text(path.read_text().replace(": true", ": 1", 1))
    changed = path.read_text()
    code, out, _ = w.run(["connect", "--undo", "vscode-copilot"])
    assert "was changed after connect; left as it is" in out
    assert json.loads(path.read_text())["github.copilot.chat.otel.enabled"] == 1 and \
        path.read_text().count("otel.enabled") == changed.count("otel.enabled")


def test_a_file_that_appears_between_preview_and_write_is_not_overwritten(w):
    from briefcase.connect import engine
    from briefcase.connect.adapters import Context

    ctx = Context(mcp=False)
    plan = engine.build_plan(["claude-code"], ctx, runner=w.cli)
    path = w.telemetry_file("claude-code")
    path.parent.mkdir(parents=True)
    path.write_text('{"late": true}')
    out = []
    assert engine.apply_plan(plan, ctx, out.append, runner=w.cli) == 1
    assert path.read_text() == '{"late": true}' and edits.load_journal()["targets"] == {}
    assert os.listdir(path.parent) == ["settings.json"]
