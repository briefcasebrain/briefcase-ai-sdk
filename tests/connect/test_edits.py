"""The edit engine: key-level merges, refusals, backups, journal, undo."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from briefcase.connect import edits, engine
from briefcase.connect.adapters import Context, registry
from briefcase.connect.edits import CHANGE, NOOP, REFUSED, FileEdit

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX paths and modes")


def say(lines):
    return lines.append


def connect(tools, runner=None, **kwargs):
    ctx = Context(**kwargs)
    plan = engine.build_plan(tools, ctx)
    out = []
    failures = engine.apply_plan(plan, ctx, out.append, runner=runner or (lambda argv: (0, "")))
    return plan, out, failures


def backups(directory):
    return sorted(p.name for p in directory.iterdir() if ".briefcase-bak-" in p.name)


def test_json_merge_preserves_unrelated_keys_order_and_indent(home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    original = {"model": "opus", "env": {"FOO": "bar"}, "permissions": {"allow": ["Bash(ls)"]}}
    settings.write_text(json.dumps(original, indent=4) + "\n")
    connect(["claude-code"], mcp=False)
    text = settings.read_text()
    after = json.loads(text)
    assert list(after) == ["model", "env", "permissions"]
    assert list(after["env"])[0] == "FOO" and after["env"]["FOO"] == "bar"
    assert after["permissions"] == original["permissions"] and after["model"] == "opus"
    assert after["env"]["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/json"
    assert '\n    "model"' in text and text.endswith("}\n")  # 4-space indent kept


def test_backup_is_written_before_the_change_and_permissions_are_kept(home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"model": "opus"}\n')
    os.chmod(settings, 0o640)
    connect(["claude-code"], mcp=False)
    (backup,) = backups(settings.parent)
    assert (settings.parent / backup).read_text() == '{"model": "opus"}\n'
    assert stat.S_IMODE(settings.stat().st_mode) == 0o640


def test_new_files_are_private_and_need_no_backup(home):
    connect(["claude-code"], mcp=False)
    settings = home / ".claude" / "settings.json"
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    assert backups(settings.parent) == []


def test_rerun_is_a_noop(home):
    connect(["claude-code", "cursor", "codex"])
    snapshot = {p: p.read_text() for p in home.rglob("*") if p.is_file()}
    plan, out, failures = connect(["claude-code", "cursor", "codex"])
    assert failures == 0 and plan.changes == [] and not plan.has_work
    assert all(r.status == NOOP for r in plan.resolved)
    assert {p: p.read_text() for p in home.rglob("*") if p.is_file() and "journal" not in p.name} == {
        p: t for p, t in snapshot.items() if "journal" not in p.name}
    assert not [p for p in home.rglob("*") if ".briefcase-bak-" in p.name]


def test_dry_run_plan_writes_nothing(home):
    plan = engine.build_plan(list(registry()), Context())
    assert plan.changes
    engine.render_plan(plan, registry(), lambda line: None)
    assert [p for p in home.rglob("*") if p.is_file()] == []


def test_undo_restores_only_our_keys_after_a_user_edit(home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"env": {"FOO": "bar", "OTEL_METRICS_EXPORTER": "console"}}, indent=2) + "\n")
    connect(["claude-code"], mcp=False)
    # The user keeps working on the file after connect.
    doc = json.loads(settings.read_text())
    doc["env"]["ADDED_LATER"] = "1"
    doc["theme"] = "dark"
    doc["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://my-own-collector"
    settings.write_text(json.dumps(doc, indent=2) + "\n")

    out = []
    assert engine.undo(None, out.append) == 1
    after = json.loads(settings.read_text())
    assert after == {
        "env": {
            "FOO": "bar",
            "OTEL_METRICS_EXPORTER": "console",  # the value from before connect is back
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://my-own-collector",  # changed by the user: left alone
            "ADDED_LATER": "1",
        },
        "theme": "dark",
    }
    assert any("was changed after connect; left as it is" in line for line in out)
    assert edits.load_journal()["files"] == {} and edits.load_journal()["tools"] == {}


def test_undo_round_trip_restores_the_original_bytes(home):
    originals = {
        home / ".claude" / "settings.json": '{\n  "model": "opus"\n}\n',
        home / ".cursor" / "hooks.json": json.dumps(
            {"version": 1, "hooks": {"stop": [{"command": "./mine.sh"}]}}, indent=2) + "\n",
        home / ".codex" / "config.toml": 'model = "gpt-5"\n\n[tui]\nnotifications = true\n',
    }
    for path, text in originals.items():
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
    connect(["claude-code", "cursor", "codex"])
    assert json.loads((home / ".cursor" / "hooks.json").read_text())["hooks"]["stop"][0] == {"command": "./mine.sh"}
    engine.undo(None, lambda line: None)
    for path, text in originals.items():
        assert path.read_text() == text, path
    assert not (home / ".cursor" / "mcp.json").exists()  # created by connect, so removed


def test_undo_of_one_tool_leaves_the_others(home):
    connect(["claude-code", "cursor"], mcp=False)
    engine.undo(["cursor"], lambda line: None)
    assert not (home / ".cursor" / "hooks.json").exists()
    assert "OTEL_LOGS_EXPORTER" in (home / ".claude" / "settings.json").read_text()
    assert set(edits.load_journal()["tools"]) == {"claude-code"}


def test_undo_dry_run_changes_nothing(home):
    connect(["claude-code"], mcp=False)
    before = (home / ".claude" / "settings.json").read_text()
    out = []
    engine.undo(None, out.append, dry_run=True)
    assert (home / ".claude" / "settings.json").read_text() == before
    assert edits.load_journal()["files"] and out and out[0].startswith("  would: ")


def test_reconnecting_with_a_new_endpoint_keeps_the_original_prior_for_undo(home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://original"}}\n')
    connect(["claude-code"], mcp=False)
    connect(["claude-code"], mcp=False, endpoint="http://127.0.0.1:5000")
    assert json.loads(settings.read_text())["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:5000"
    engine.undo(None, lambda line: None)
    assert json.loads(settings.read_text()) == {"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://original"}}


def test_cursor_hook_is_replaced_not_duplicated_when_flags_change(home):
    connect(["cursor"], mcp=False)
    connect(["cursor"], mcp=False, endpoint="http://127.0.0.1:5000")
    hooks = json.loads((home / ".cursor" / "hooks.json").read_text())["hooks"]
    assert all(len(entries) == 1 for entries in hooks.values())
    assert hooks["stop"][0]["command"].endswith("--endpoint http://127.0.0.1:5000")
    engine.undo(None, lambda line: None)
    assert not (home / ".cursor" / "hooks.json").exists()


def test_a_4_4_cursor_config_naming_the_old_commands_is_repaired(home, fake_bin):
    fake_bin("briefcase-ai")
    cursor = home / ".cursor"
    cursor.mkdir()
    old_hook = {"command": "briefcase hook cursor", "timeout": 5}
    (cursor / "hooks.json").write_text(json.dumps({"version": 1, "hooks": {"stop": [old_hook]}}))
    (cursor / "mcp.json").write_text(json.dumps({"mcpServers": {"briefcase": {"command": "briefcase-mcp"}}}))
    connect(["cursor"])
    hooks = json.loads((cursor / "hooks.json").read_text())["hooks"]
    assert [entry["command"] for entry in hooks["stop"]] == ["briefcase-ai hook cursor"]
    servers = json.loads((cursor / "mcp.json").read_text())["mcpServers"]
    assert "briefcase-mcp" not in json.dumps(servers) and "briefcase-ai-mcp" in json.dumps(servers)


class FakeClaudeMcp:
    """Behaves like `claude mcp add|remove|get` (2.1.330, checked with a throwaway HOME): add of an
    existing name exits 1 with "already exists"; remove of a missing name exits 1; get prints the
    command and args. ``fail_add`` makes the next add of the new launcher fail."""

    def __init__(self, servers, fail_new_add=False):
        self.servers = dict(servers)
        self.calls = []
        self.fail_new_add = fail_new_add

    def __call__(self, argv):
        self.calls.append(argv)
        verb = argv[2]
        if verb == "get":
            name = argv[3]
            if name not in self.servers:
                return 1, f'No MCP server found with name: "{name}"'
            command = self.servers[name]
            return 0, f"{name}:\n  Type: stdio\n  Command: {command[0]}\n  Args: {' '.join(command[1:])}\n"
        name = argv[5]
        if verb == "add":
            if name in self.servers:
                return 1, f"MCP server {name} already exists in user config"
            tail = argv[argv.index("--") + 1:]
            if self.fail_new_add and tail[-1] == "briefcase-ai-mcp":
                return 1, "simulated failure"
            self.servers[name] = tail
            return 0, ""
        if name not in self.servers:
            return 1, f'No MCP server named "{name}" in user scope'
        del self.servers[name]
        return 0, ""


def test_a_4_4_claude_code_mcp_entry_is_replaced_not_refused(home, fake_bin):
    fake_bin("claude", "2.1.330 (Claude Code)")
    old_argv = ["claude", "mcp", "add", "--scope", "user", "briefcase", "--",
                "uvx", "--from", "briefcase-ai[mcp]", "briefcase-mcp"]
    journal = edits.load_journal()
    journal["commands"].append({"tool": "claude-code", "argv": old_argv, "undo_hint": None, "at": "2026-08-01"})
    edits.save_journal(journal)
    claude = FakeClaudeMcp({"briefcase": old_argv[7:]})
    plan, out, failures = connect(["claude-code"], runner=claude)
    assert failures == 0, out
    assert claude.servers["briefcase"][-1] == "briefcase-ai-mcp"
    assert [c for c in edits.load_journal()["commands"] if c["argv"] == old_argv] == []


def test_a_user_managed_server_named_briefcase_is_left_alone(home, fake_bin):
    fake_bin("claude", "2.1.330 (Claude Code)")
    claude = FakeClaudeMcp({"briefcase": ["my-own-server", "--flag"]})
    plan, out, failures = connect(["claude-code"], runner=claude)
    assert failures == 0
    assert claude.servers["briefcase"] == ["my-own-server", "--flag"]
    assert not any(call[2] == "remove" for call in claude.calls)
    assert any("not created by briefcase" in line for line in out), out


def test_a_failed_re_add_restores_the_previous_registration(home, fake_bin):
    fake_bin("claude", "2.1.330 (Claude Code)")
    old = ["uvx", "--from", "briefcase-ai[mcp]", "briefcase-mcp"]
    claude = FakeClaudeMcp({"briefcase": old}, fail_new_add=True)
    plan, out, failures = connect(["claude-code"], runner=claude)
    assert failures == 1
    assert claude.servers["briefcase"] == old  # put back, not left missing


def test_claude_code_mcp_first_install_does_not_remove_anything(home, fake_bin):
    fake_bin("claude", "2.1.330 (Claude Code)")
    claude = FakeClaudeMcp({})
    connect(["claude-code"], runner=claude)
    assert [c[2] for c in claude.calls] == ["add"]


@pytest.mark.parametrize("body, why", [
    ('{"env": {"A": "1",}', "not valid JSON"),
    ('{\n  // my settings\n  "env": {"A": "1"}\n}\n', "comments or trailing commas"),
    ('{"env": {"A": "1"},}\n', "comments or trailing commas"),
    ('{"a": 1, /* note */ "b": 2}', "comments or trailing commas"),
    ("[1, 2]", "JSON object at the top level"),
    ('{"env": "not-an-object"}', "expected an object at 'env'"),
])
def test_malformed_or_jsonc_input_refuses_to_write(home, body, why):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(body)
    plan, out, failures = connect(["claude-code"], mcp=False)
    (resolved,) = plan.resolved
    assert resolved.status == REFUSED and why in resolved.reason
    assert settings.read_text() == body and backups(settings.parent) == []
    # The snippet is offered instead.
    (manual,) = plan.manual
    assert '"CLAUDE_CODE_ENABLE_TELEMETRY": "1"' in manual.text and why in manual.reason
    assert edits.load_journal()["files"] == {}


def test_a_url_containing_slashes_is_not_mistaken_for_a_comment(home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"env": {"URL": "https://example.com/a//b"}}\n')
    plan, _, _ = connect(["claude-code"], mcp=False)
    assert plan.resolved[0].status == CHANGE


def test_toml_block_is_appended_and_replaced_in_place(home):
    config = home / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('model = "gpt-5"\n')
    connect(["codex"])
    text = config.read_text()
    assert text.startswith('model = "gpt-5"\n\n# >>> briefcase >>>\n') and text.endswith("# <<< briefcase <<<\n")
    config.write_text(text + '\n[profiles.fast]\nmodel = "mini"\n')
    connect(["codex"], endpoint="http://127.0.0.1:5000")
    text = config.read_text()
    assert text.count("# >>> briefcase >>>") == 1 and "127.0.0.1:5000/v1/logs" in text
    assert text.index("# <<< briefcase <<<") < text.index("[profiles.fast]")
    engine.undo(None, lambda line: None)
    assert config.read_text() == 'model = "gpt-5"\n\n[profiles.fast]\nmodel = "mini"\n'


@pytest.mark.parametrize("body, why", [
    ('[otel]\nenvironment = "prod"\n', "already defines [otel] outside the briefcase block"),
    ('[mcp_servers.briefcase]\ncommand = "x"\n', "already defines [mcp_servers.briefcase]"),
    ('model = \n', "not valid TOML"),
])
def test_toml_refuses_existing_table_or_malformed_file(home, body, why):
    config = home / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(body)
    plan, _, _ = connect(["codex"])
    assert plan.resolved[0].status == REFUSED and why in plan.resolved[0].reason
    assert config.read_text() == body
    assert "[otel]" in plan.manual[0].text


def test_commands_run_once_as_argument_lists_and_are_journalled(home, fake_bin):
    fake_bin("claude", "2.1.330 (Claude Code)")
    calls = []

    def runner(argv):
        calls.append(argv)
        return 0, ""

    plan, out, failures = connect(["claude-code"], runner=runner)
    assert failures == 0
    assert calls == [["claude", "mcp", "add", "--scope", "user", "briefcase", "--",
                      "uvx", "--from", "briefcase-ai[mcp]", "briefcase-ai-mcp"]]
    plan, _, _ = connect(["claude-code"], runner=runner)
    assert len(calls) == 1 and plan.commands == [] and len(plan.done_commands) == 1
    out = []
    engine.undo(None, out.append)
    assert any("claude mcp remove --scope user briefcase" in line for line in out)
    assert edits.load_journal()["commands"] == []


def test_a_failing_command_is_reported_and_not_journalled(home, fake_bin):
    fake_bin("claude", "2.1.330 (Claude Code)")
    plan, out, failures = connect(["claude-code"], runner=lambda argv: (1, "already exists"))
    assert failures == 1 and any("already exists" in line for line in out)
    assert edits.load_journal()["commands"] == []


def test_file_changed_between_plan_and_apply_is_not_overwritten(home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"a": 1}\n')
    ctx = Context(mcp=False)
    plan = engine.build_plan(["claude-code"], ctx)
    settings.write_text('{"a": 2}\n')
    out = []
    assert engine.apply_plan(plan, ctx, out.append) == 1
    assert settings.read_text() == '{"a": 2}\n' and "changed while the plan was on screen" in out[0]


def test_symlink_out_of_home_is_followed_only_with_a_warning(home, tmp_path):
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    real = dotfiles / "claude-settings.json"
    real.write_text('{"model": "opus"}\n')
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").symlink_to(real)
    plan, _, _ = connect(["claude-code"], mcp=False)
    (resolved,) = plan.resolved
    assert any("outside your home directory" in w for w in resolved.warnings)
    assert (home / ".claude" / "settings.json").is_symlink()  # the link itself is not replaced
    assert json.loads(real.read_text())["env"]["OTEL_LOGS_EXPORTER"] == "otlp"


@pytest.mark.parametrize("path", ["/etc/claude-code/managed-settings.json",
                                  "/Library/Application Support/ClaudeCode/managed-settings.json"])
def test_system_paths_are_never_written(home, path):
    from pathlib import Path

    assert edits.is_system_path(Path(path))
    resolved = edits.resolve(FileEdit("claude-code", Path(path), "json", "x",
                                      ops=[{"op": "set", "path": ["env", "A"], "value": "1"}]))
    assert resolved.status == REFUSED and "managed/system path" in resolved.reason


@pytest.mark.parametrize("path", ["/var/db/briefcase-test.json", "/var/root/briefcase-test.json"])
def test_var_system_locations_are_refused_even_where_var_is_a_symlink(home, path):
    from pathlib import Path

    resolved = edits.resolve(FileEdit("claude-code", Path(path), "json", "x",
                                      ops=[{"op": "set", "path": ["env", "A"], "value": "1"}]))
    assert resolved.status == REFUSED, resolved.reason


def test_the_working_directory_does_not_exempt_a_system_path(home, monkeypatch):
    from pathlib import Path

    monkeypatch.chdir("/etc")
    resolved = edits.resolve(FileEdit("claude-code", Path("/etc/claude-code/managed-settings.json"), "json", "x",
                                      ops=[{"op": "set", "path": ["env", "A"], "value": "1"}]))
    assert resolved.status == REFUSED


def test_only_the_users_own_temp_directory_is_exempt(tmp_path):
    assert not edits.is_system_path(tmp_path / "x.json")
    assert edits.is_system_path(Path("/etc/x.json"))
    assert edits.is_system_path(Path("/var/db/x.json"))
    assert not edits.is_system_path(Path.home() / ".claude" / "settings.json")


def test_diff_masks_secret_looking_values(home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-ant-secret"}}, indent=2) + "\n")
    plan = engine.build_plan(["claude-code"], Context(mcp=False, headers={"Authorization": "Bearer tok-123"}))
    diff = edits.render_diff(plan.resolved[0])
    assert "sk-ant-secret" not in diff and "tok-123" not in diff
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in diff and "http://127.0.0.1:4318" in diff
    codex = engine.build_plan(["codex"], Context(mcp=False, headers={"Authorization": "Bearer tok-123"}))
    assert "tok-123" not in edits.render_diff(codex.resolved[0])


def test_status_and_doctor_checks_report_drift(home, fake_bin):
    assert engine.doctor_checks(probe=lambda e: None) == [
        ("warn", "connect", "no tools connected yet; run: briefcase-ai connect")]
    fake_bin("claude", "2.1.330 (Claude Code)")
    connect(["claude-code", "codex"], mcp=False)
    rows = {row["tool"]: row for row in engine.status()}
    assert rows["claude-code"]["connected"] and all(ok for ok, _ in rows["claude-code"]["checks"])
    assert not rows["cursor"]["connected"]

    results = engine.doctor_checks(probe=lambda endpoint: {"protobuf": False})
    by_label = {label: (sev, detail) for sev, label, detail in results}
    assert by_label["connect claude-code"][0] == "ok"
    assert by_label["connect claude-code version"] == ("ok", "2.1.330 (in supported range)")
    assert by_label["receiver"] == ("ok", "reachable at http://127.0.0.1:4318")
    assert by_label["receiver protobuf"][0] == "warn" and "Codex CLI" in by_label["receiver protobuf"][1]

    settings = home / ".claude" / "settings.json"
    doc = json.loads(settings.read_text())
    del doc["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"]
    settings.write_text(json.dumps(doc))
    fake_bin("claude", "9.1.0 (Claude Code)")
    results = engine.doctor_checks(probe=lambda endpoint: None)
    by_label = {label: (sev, detail) for sev, label, detail in results}
    assert by_label["connect claude-code"][0] == "fail"
    assert "env.CLAUDE_CODE_ENABLE_TELEMETRY" in by_label["connect claude-code"][1]
    assert by_label["connect claude-code version"][0] == "warn"
    assert by_label["receiver"][0] == "warn"


def test_doctor_reports_managed_settings_override(home, tmp_path):
    connect(["claude-code"], mcp=False)
    managed = tmp_path / "managed-settings.json"
    managed.write_text(json.dumps({"env": {"OTEL_LOGS_EXPORTER": "none"}}))
    adapters = registry()
    adapters["claude-code"].data["managed_settings"] = [str(managed)]
    adapters["claude-code"].data["managed_settings_dirs"] = []
    results = engine.doctor_checks(adapters, probe=lambda endpoint: {"protobuf": True})
    (row,) = [r for r in results if r[1] == "connect claude-code managed settings"]
    assert row[0] == "warn" and "OTEL_LOGS_EXPORTER" in row[2]
