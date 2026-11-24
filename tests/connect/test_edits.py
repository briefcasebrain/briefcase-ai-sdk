"""The edit engine: key-level merges, refusals, backups, journal, undo."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from briefcase.connect import edits, engine
from briefcase.connect.adapters import Context, registry
from briefcase.connect.edits import NOOP, REFUSED, FileEdit

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX paths and modes")


def say(lines):
    return lines.append


def connect(tools, runner=None, **kwargs):
    ctx = Context(**kwargs)
    plan = engine.build_plan(tools, ctx, runner=runner or (lambda argv: (0, "")))
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


def test_undo_removes_only_the_keys_that_still_match_after_a_user_edit(home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"env": {"FOO": "bar"}}, indent=2) + "\n")
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
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://my-own-collector",  # changed by the user: left alone
            "ADDED_LATER": "1",
        },
        "theme": "dark",
    }
    assert any("was changed after connect; left as it is" in line for line in out)
    assert edits.load_journal()["files"] == {} and edits.load_journal()["targets"] == {}


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
    assert {t["tool"] for t in edits.load_journal()["targets"].values()} == {"claude-code"}


def test_undo_dry_run_changes_nothing(home):
    connect(["claude-code"], mcp=False)
    before = (home / ".claude" / "settings.json").read_text()
    out = []
    engine.undo(None, out.append, dry_run=True)
    assert (home / ".claude" / "settings.json").read_text() == before
    assert edits.load_journal()["files"] and out and out[0].startswith("  would: ")


def test_a_key_the_user_already_set_is_left_alone_not_overwritten_and_not_removed_by_undo(home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    original = '{"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://original"}}\n'
    settings.write_text(original)
    plan, out, failures = connect(["claude-code"], mcp=False)
    assert settings.read_text() == original and [b.blocker for b in plan.blocked] == ["foreign"]
    assert not plan.has_work and edits.load_journal()["targets"] == {}
    engine.undo(None, lambda line: None)
    assert settings.read_text() == original


def test_a_cursor_hook_is_not_rewritten_when_the_flags_change(home):
    connect(["cursor"], mcp=False)
    before = (home / ".cursor" / "hooks.json").read_text()
    plan, out, failures = connect(["cursor"], mcp=False, endpoint="http://127.0.0.1:5000",
                                  given={"endpoint": "http://127.0.0.1:5000"})
    assert [b.blocker for b in plan.blocked] == ["differs"] and plan.differs and not plan.has_work
    assert (home / ".cursor" / "hooks.json").read_text() == before
    engine.undo(None, lambda line: None)
    assert not (home / ".cursor" / "hooks.json").exists()


def write_claude_json(tail):
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    path = (Path(base) if base else edits.home()) / ".claude.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"briefcase": {"command": tail[0], "args": list(tail[1:])}}}))


class FakeClaude:
    """Behaves like `claude mcp add|remove` (2.1.330, checked with a throwaway HOME): add of a name that
    already exists in the same scope exits 1 with "already exists"; remove of a missing name exits 1.
    Registrations are keyed by (config dir, scope, cwd-for-non-user scopes), as Claude Code stores them."""

    def __init__(self, existing=(), fail_add=False):
        self.registered = {key: ["old-command"] for key in existing}
        self.calls = []
        self.fail_add = fail_add

    def _key(self, scope):
        return (os.environ.get("CLAUDE_CONFIG_DIR"), scope, os.getcwd() if scope != "user" else None)

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[2] == "get":
            for key, tail in self.registered.items():
                if key[0] == os.environ.get("CLAUDE_CONFIG_DIR"):
                    write_claude_json(tail)  # Claude's own file holds the argument list exactly
                    head = "briefcase:\n  Scope: User config (available in all your projects)\n  Type: stdio\n"
                    return 0, head + "  Command: %s\n  Args: %s" % (tail[0], " ".join(tail[1:]))
            return 1, 'No MCP server found with name: "briefcase"'
        verb, scope = argv[2], argv[4]
        key = self._key(scope)
        if verb == "add":
            if key in self.registered:
                return 1, "MCP server briefcase already exists in %s config" % scope
            if self.fail_add:
                return 1, "simulated failure"
            self.registered[key] = argv[argv.index("--") + 1:]
            return 0, ""
        if key not in self.registered:
            return 1, 'No MCP server named "briefcase" in %s scope' % scope
        del self.registered[key]
        return 0, ""

    def removed(self):
        return [c for c in self.calls if c[2] == "remove"]

    def added(self):
        return [c for c in self.calls if c[2] == "add"]


def journal_registration(scope="user", config_dir=None, cwd=None, tail=("old-command",)):
    """What an earlier `connect` of this SDK recorded for its own registration."""
    argv = ["claude", "mcp", "add", "--scope", scope, "briefcase", "--", *tail]
    ident = {"profile": config_dir, "cwd": cwd}
    journal = edits.load_journal()
    journal["commands"].append({"tool": "claude-code", "argv": argv, "undo_hint": None, "at": "2026-08-01", **ident})
    target = engine.command_target(argv, ident)
    journal["targets"][engine.target_key("claude-code", "mcp", target)] = {
        "tool": "claude-code", "kind": "mcp", "scope": scope, "target": target, "argv": argv, "undo_hint": None,
        "endpoint": "http://127.0.0.1:4318", "headers_sha": "", "capture_content": False}
    edits.save_journal(journal)


def test_a_journalled_registration_the_cli_still_reports_is_already_connected_and_never_replaced(home, fake_bin):
    fake_bin("claude", "2.1.330 (Claude Code)")
    journal_registration("user")
    claude = FakeClaude([(None, "user", None)])
    plan, out, failures = connect(["claude-code"], runner=claude)
    assert failures == 0 and not claude.added() and not claude.removed()
    assert claude.registered[(None, "user", None)] == ["old-command"] and len(plan.done_commands) == 1


def test_a_journalled_registration_that_was_changed_in_the_cli_is_not_ours_any_more(home, fake_bin):
    fake_bin("claude", "2.1.330 (Claude Code)")
    journal_registration("user")
    claude = FakeClaude([(None, "user", None)])
    claude.registered[(None, "user", None)] = ["node", "server.js", "--token=SECRET-VALUE"]
    plan, out, failures = connect(["claude-code"], runner=claude)
    text = "\n".join(out + [b.text for b in plan.blocked])
    assert failures == 0 and not claude.added() and not claude.removed() and "SECRET-VALUE" not in text
    assert "no longer treated as briefcase's" in text and [b.blocker for b in plan.blocked] == ["modified"]


def test_a_journalled_registration_that_the_cli_no_longer_has_is_not_re_added(home, fake_bin):
    fake_bin("claude", "2.1.330 (Claude Code)")
    journal_registration("user")
    claude = FakeClaude([])
    plan, out, failures = connect(["claude-code"], runner=claude)
    assert failures == 0 and not claude.added() and [b.blocker for b in plan.blocked] == ["modified"]


def test_an_unjournalled_registration_is_skipped_untouched_silent_and_not_a_failure(home, fake_bin):
    fake_bin("claude", "2.1.330 (Claude Code)")
    claude = FakeClaude([(None, "user", None)])
    claude.registered[(None, "user", None)] = ["node", "server.js", "--token=SECRET-VALUE"]
    plan, out, failures = connect(["claude-code"], runner=claude)
    assert failures == 0 and not claude.removed() and not claude.added()  # looked first, never ran the add
    assert claude.registered[(None, "user", None)] == ["node", "server.js", "--token=SECRET-VALUE"]
    text = "\n".join(out + [b.text for b in plan.blocked])
    assert "SECRET-VALUE" not in text and "server.js" not in text
    assert "'briefcase'" in text and "did not write it" in text
    assert [b.blocker for b in plan.blocked if b.kind == "mcp"] == ["foreign"]


def test_mcp_install_still_fails_when_it_cannot_register(home, fake_bin, monkeypatch, capsys):
    from argparse import Namespace

    from briefcase.cli import mcp_commands

    fake_bin("claude", "2.1.330 (Claude Code)")
    claude = FakeClaude([(None, "user", None)])
    monkeypatch.setattr(engine, "run_command", claude)
    args = Namespace(tools=["claude-code"], scope="user", yes=True, connect_dry_run=False, dry_run=False,
                     force=False, headers=[])
    assert mcp_commands.cmd_mcp_install(args) == 1
    assert "did not write it" in capsys.readouterr().out and not claude.added()


def test_a_registration_journalled_for_another_profile_is_not_ours_here(home, fake_bin, monkeypatch, tmp_path):
    fake_bin("claude", "2.1.330 (Claude Code)")
    profile_a, profile_b = str(tmp_path / "a"), str(tmp_path / "b")
    journal_registration("user", config_dir=profile_a)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", profile_b)
    claude = FakeClaude([(profile_b, "user", None)])
    plan, out, failures = connect(["claude-code"], runner=claude)
    # Profile B holds an entry nobody journalled for it: skipped, untouched, and no add was attempted.
    assert failures == 0 and not claude.removed() and not claude.added()
    assert claude.registered[(profile_b, "user", None)] == ["old-command"]
    assert [b.blocker for b in plan.blocked if b.kind == "mcp"] == ["foreign"]
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", profile_a)
    claude = FakeClaude([(profile_a, "user", None)])
    plan, out, failures = connect(["claude-code"], runner=claude)
    assert failures == 0 and not claude.added() and len(plan.done_commands) == 1


def test_claude_code_mcp_first_install_does_not_remove_anything(home, fake_bin):
    fake_bin("claude", "2.1.330 (Claude Code)")
    claude = FakeClaude([])
    connect(["claude-code"], runner=claude)
    assert [c[2] for c in claude.calls if c[2] != "get"] == ["add"]
    entry = [c for c in edits.load_journal()["commands"] if c["tool"] == "claude-code"][0]
    assert entry["profile"] is None and entry["cwd"] is None


class FakeCodex:
    """`codex mcp add NAME -- CMD ...` replaces the named global entry silently."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        return 0, ""


def test_a_codex_registration_is_not_reissued_when_the_directory_changes(home, fake_bin, tmp_path, monkeypatch):
    fake_bin("codex", "codex 0.171.0")
    codex = FakeCodex()
    connect(["codex"], runner=codex)
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    plan, out, failures = connect(["codex"], runner=codex)
    assert [c[2] for c in codex.calls if c[2] == "add"] == ["add"] and failures == 0 and not plan.commands


def test_toml_block_is_appended_once_and_never_rewritten(home):
    config = home / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('model = "gpt-5"\n')
    connect(["codex"])
    text = config.read_text()
    assert text.startswith('model = "gpt-5"\n\n# >>> briefcase >>>\n') and text.endswith("# <<< briefcase <<<\n")
    config.write_text(text + '\n[profiles.fast]\nmodel = "mini"\n')
    plan, _, _ = connect(["codex"], endpoint="http://127.0.0.1:5000", given={"endpoint": "http://127.0.0.1:5000"})
    assert plan.differs and config.read_text() == text + '\n[profiles.fast]\nmodel = "mini"\n'
    engine.undo(None, lambda line: None)
    assert config.read_text() == 'model = "gpt-5"\n\n[profiles.fast]\nmodel = "mini"\n'


@pytest.mark.parametrize("body", ['[otel]\nenvironment = "prod"\n', '[mcp_servers.briefcase]\ncommand = "x"\n'])
def test_toml_tables_the_user_already_defined_are_foreign_untouched_and_unprinted(home, body):
    config = home / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(body)
    plan, out, failures = connect(["codex"])
    assert [b.blocker for b in plan.blocked] == ["foreign"] and not plan.changes
    assert config.read_text() == body and "prod" not in "\n".join(b.text for b in plan.blocked)


def test_malformed_toml_is_refused_and_the_snippet_is_offered(home):
    config = home / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text("model = \n")
    plan, _, _ = connect(["codex"])
    assert plan.resolved[0].status == REFUSED and "not valid TOML" in plan.resolved[0].reason
    assert config.read_text() == "model = \n" and "[otel]" in plan.manual[0].text


def test_commands_run_once_as_argument_lists_and_are_journalled(home, fake_bin):
    fake_bin("claude", "2.1.330 (Claude Code)")
    calls = []

    def runner(argv):
        calls.append(argv)
        if argv[2] == "get":
            if not any(c[2] == "add" for c in calls):
                return 1, 'No MCP server found with name: "briefcase"'
            write_claude_json(["uvx", "--from", "briefcase-ai[mcp]", "briefcase-ai-mcp"])
            head = "briefcase:\n  Scope: User config (available in all your projects)\n  Type: stdio\n"
            return 0, head + "  Command: uvx\n  Args: --from briefcase-ai[mcp] briefcase-ai-mcp"
        return 0, ""

    plan, out, failures = connect(["claude-code"], runner=runner)
    assert failures == 0
    add = ["claude", "mcp", "add", "--scope", "user", "briefcase", "--", "uvx", "--from", "briefcase-ai[mcp]",
           "briefcase-ai-mcp"]
    assert [c for c in calls if c[2] != "get"] == [add]
    plan, _, _ = connect(["claude-code"], runner=runner)
    assert len([c for c in calls if c[2] == "add"]) == 1 and plan.commands == [] and len(plan.done_commands) == 1
    out = []
    engine.undo(None, out.append, runner=runner)
    assert any("claude mcp remove --scope user briefcase" in line for line in out)
    assert edits.load_journal()["commands"] == []


def test_a_failing_command_is_reported_and_not_journalled(home, fake_bin):
    fake_bin("claude", "2.1.330 (Claude Code)")
    plan, out, failures = connect(["claude-code"], runner=lambda argv: (1, "already exists"))
    assert failures == 0 and any("already registered" in line for line in out)
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
    assert settings.read_text() == '{"a": 2}\n' and "changed after the preview" in out[0]


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


@pytest.mark.parametrize("path", [
    "/etc/x.json", "/private/etc/x.json", "/System/x", "/Library/Application Support/ClaudeCode/x.json",
    "/usr/bin/x", "/usr/lib/x", "/bin/x", "/sbin/x", "/var/db/x", "/var/root/x", "/private/var/db/x",
    "/private/var/root/x", "/etc/claude-code/managed-settings.d/10.json", "/etc/codex/hooks.json",
])
def test_the_listed_system_locations_are_refused(path):
    assert edits.is_system_path(Path(path))


@pytest.mark.parametrize("path", ["/opt/project/.mcp.json", "/var/tmp/project/.mcp.json", "/usr/local/src/p/x.json",
                                  "/srv/app/x.json", "/tmp/project/x.json"])
def test_ordinary_project_locations_are_accepted(path):
    assert not edits.is_system_path(Path(path))


def test_temp_dir_settings_are_not_an_exemption(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", "/etc")  # a hostile TMPDIR must not whitelist anything
    assert edits.is_system_path(Path("/etc/x.json"))
    assert not edits.is_system_path(tmp_path / "x.json")  # allowed because it is not on the list, not via TMPDIR


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
