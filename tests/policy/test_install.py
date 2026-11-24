"""Installing and removing the enforcement hooks through the connect edit/journal machinery,
in a temp HOME: idempotency, undo, coexistence with the user's own hooks and with
`briefcase-ai connect`, and how the installed command behaves when its launcher is gone."""

import json
import os
import shlex
import subprocess
import sys
from argparse import Namespace

import pytest

from briefcase.policy import install

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX paths")


def init_args(**overrides):
    args = dict(tools=["claude-code", "cursor", "codex"], preset=None, scope="user", fail_closed=False,
                portable=False, capture_content=False, endpoint=None, force=False, policy_dry_run=False,
                dry_run=False, yes=True)
    args.update(overrides)
    return Namespace(**args)


def off_args(**overrides):
    args = dict(tools=[], policy_dry_run=False, yes=True)
    args.update(overrides)
    return Namespace(**args)


def init(**overrides):
    from briefcase.cli import policy_commands
    lines = []
    code = policy_commands.run_init(init_args(**overrides), out=lines.append)
    return code, "\n".join(lines)


def off(capsys, **overrides):
    from briefcase.cli import policy_commands
    code = policy_commands.cmd_off(off_args(**overrides))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def files(home):
    return {
        "claude": home / ".claude" / "settings.json",
        "cursor": home / ".cursor" / "hooks.json",
        "codex": home / ".codex" / "hooks.json",
    }


def snapshot(home):
    return {name: (path.read_text() if path.exists() else None) for name, path in files(home).items()}


def test_init_writes_policy_and_every_hook_in_the_documented_shape(home):
    code, out = init()
    assert code == 0, out
    assert (home / ".briefcase" / "policy.toml").exists()
    assert oct((home / ".briefcase" / "policy.toml").stat().st_mode & 0o777) == "0o600"
    claude = json.loads(files(home)["claude"].read_text())
    (entry,) = claude["hooks"]["PreToolUse"]
    assert entry["matcher"].startswith("^(Bash|")
    (handler,) = entry["hooks"]
    assert set(handler) == {"type", "command", "timeout"} and handler["type"] == "command"
    assert " claude-code --enforce" in handler["command"]
    cursor = json.loads(files(home)["cursor"].read_text())
    assert cursor["version"] == 1
    assert set(cursor["hooks"]) == {"beforeShellExecution", "beforeMCPExecution", "beforeReadFile", "preToolUse"}
    assert cursor["hooks"]["preToolUse"][0]["matcher"] == "Write|Delete"
    assert all(set(e[0]) <= {"command", "timeout", "matcher"} for e in cursor["hooks"].values())
    codex = json.loads(files(home)["codex"].read_text())
    assert codex["hooks"]["PreToolUse"][0]["matcher"] == "^(Bash|apply_patch)$|^mcp__"
    assert "Next: briefcase-ai policy test" in out


def test_init_twice_changes_nothing_and_off_twice_is_harmless(home, capsys):
    assert init()[0] == 0
    first = snapshot(home)
    code, out = init()
    assert code == 0 and "Nothing to write" in out
    assert snapshot(home) == first
    assert off(capsys)[0] == 0
    assert snapshot(home) == {"claude": None, "cursor": None, "codex": None}   # briefcase created them
    code, out = off(capsys)
    assert code == 0 and "No policy hooks are installed" in out


def test_off_keeps_the_users_own_settings_and_hooks(home, capsys):
    settings = files(home)["claude"]
    settings.parent.mkdir(parents=True)
    mine = {"model": "opus", "hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-own-check.sh"}]}]}}
    settings.write_text(json.dumps(mine, indent=2))
    assert init(tools=["claude-code"])[0] == 0
    doc = json.loads(settings.read_text())
    assert doc["model"] == "opus" and len(doc["hooks"]["PreToolUse"]) == 2
    assert doc["hooks"]["PreToolUse"][0] == mine["hooks"]["PreToolUse"][0]
    assert off(capsys)[0] == 0
    assert json.loads(settings.read_text()) == mine
    assert list(settings.parent.glob("settings.json.briefcase-bak-*")), "a backup is kept"


def test_reinstall_with_other_settings_changes_nothing_until_off(home, capsys, fake_bin):
    fake_bin("claude", "2.1.330 (Claude Code)")
    assert init(tools=["claude-code"])[0] == 0
    before, policy = files(home)["claude"].read_text(), (home / ".briefcase" / "policy.toml").read_text()
    code, out = init(tools=["claude-code"], fail_closed=True, force=True)
    assert code == 1 and "briefcase-ai policy off claude-code" in out
    assert files(home)["claude"].read_text() == before  # the hook is not rewritten
    assert (home / ".briefcase" / "policy.toml").read_text() == policy  # nor the policy replaced
    assert not list((home / ".briefcase").glob("policy.toml.briefcase-bak-*"))
    assert off(capsys)[0] == 0
    assert not files(home)["claude"].exists()
    assert init(tools=["claude-code"], fail_closed=True, force=True)[0] == 0
    (entry,) = json.loads(files(home)["claude"].read_text())["hooks"]["PreToolUse"]
    handler = entry["hooks"][0]
    assert handler["onFailure"] == "block" and "--enforce --fail-closed" in handler["command"]
    assert "fail_closed = true" in (home / ".briefcase" / "policy.toml").read_text()


def test_fail_closed_is_refused_where_the_claude_version_is_unknown(home):
    code, out = init(tools=["claude-code"], fail_closed=True)  # no `claude` on PATH: the version cannot be read
    assert code == 1 and "could not be read" in out and not files(home)["claude"].exists()


def test_fail_closed_for_codex_says_plainly_what_it_cannot_do_and_needs_an_acknowledgement(home, fake_bin):
    code, out = init(tools=["codex"], fail_closed=True)
    assert code == 1 and "cannot block" in out and "--accept-codex-startup-risk" in out
    assert not files(home)["codex"].exists()
    code, out = init(tools=["codex"], fail_closed=True, accept_codex_startup_risk=True)
    assert code == 0 and "cannot block" in out and files(home)["codex"].exists()


def test_fail_closed_is_refused_for_claude_code_older_than_documented(home, fake_bin):
    fake_bin("claude", "2.1.200 (Claude Code)")
    code, out = init(tools=["claude-code"], fail_closed=True)
    assert code == 1 and "needs Claude Code" in out and not files(home)["claude"].exists()


def test_off_that_cannot_remove_a_hook_says_enforcement_is_still_on_and_keeps_the_record(home, capsys):
    from briefcase.policy import install

    assert init(tools=["claude-code"])[0] == 0
    settings = files(home)["claude"]
    doc = json.loads(settings.read_text())
    doc["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] = 30
    settings.write_text(json.dumps(doc))
    edited = settings.read_text()
    code, out = off(capsys)
    assert code == 1 and "Enforcement is still ON" in out
    assert "Enforcement is off" not in out
    assert settings.read_text() == edited and [r["tool"] for r in install.installed()] == ["claude-code"]


def test_policy_off_leaves_connect_telemetry_and_connect_undo_removes_everything(home, capsys):
    from briefcase.connect import engine

    (home / ".cursor").mkdir()
    connect_plan_tools = ["cursor"]
    from briefcase.connect.adapters import Context, registry
    adapters = registry()
    ctx = Context(mcp=False)
    plan = engine.build_plan(connect_plan_tools, ctx, adapters)
    assert engine.apply_plan(plan, ctx, lambda line: None) == 0
    telemetry = json.loads(files(home)["cursor"].read_text())
    assert init(tools=["cursor"])[0] == 0
    both = json.loads(files(home)["cursor"].read_text())
    assert set(both["hooks"]) > set(telemetry["hooks"])
    assert off(capsys)[0] == 0
    assert json.loads(files(home)["cursor"].read_text()) == telemetry
    assert init(tools=["cursor"])[0] == 0
    engine.undo(None, lambda line: None)
    assert not files(home)["cursor"].exists()


def test_dry_run_writes_nothing(home):
    code, out = init(policy_dry_run=True)
    assert code == 0 and "Dry run" in out
    assert not (home / ".briefcase" / "policy.toml").exists()
    assert snapshot(home) == {"claude": None, "cursor": None, "codex": None}


def test_without_yes_and_without_a_terminal_nothing_is_written(home, monkeypatch, capsys):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    code, _ = init(yes=False)
    assert code == 1 and "--yes" in capsys.readouterr().err
    assert not (home / ".briefcase" / "policy.toml").exists()


def test_an_invalid_existing_policy_is_never_overwritten_without_force(home, capsys):
    path = home / ".briefcase" / "policy.toml"
    path.parent.mkdir()
    path.write_text("version = 1\nnonsense = 1\n")
    code, _ = init()
    assert code == 1 and "briefcase-ai policy init --force" in capsys.readouterr().err
    assert path.read_text() == "version = 1\nnonsense = 1\n"


def test_project_scope_writes_project_files_with_the_portable_command(home, project):
    code, out = init(tools=["claude-code", "cursor", "codex"], scope="project")
    assert code == 0, out
    assert (project / ".briefcase" / "policy.toml").exists()
    handler = json.loads((project / ".claude" / "settings.json").read_text())["hooks"]["PreToolUse"][0]["hooks"][0]
    assert handler["command"] == "briefcase-ai hook claude-code --enforce --endpoint http://127.0.0.1:4318"
    assert (project / ".cursor" / "hooks.json").exists() and (project / ".codex" / "hooks.json").exists()
    assert not (home / ".claude" / "settings.json").exists()


def test_status_and_doctor_see_a_hook_that_was_removed_by_hand(home):
    from briefcase.cli import policy_commands
    assert init(tools=["claude-code"])[0] == 0
    assert policy_commands.status()["state"] == "on"
    files(home)["claude"].write_text("{}")
    row = policy_commands.status()
    assert row["state"] == "attention" and "no longer present" in row["detail"]
    assert any(sev == "fail" for sev, _, _ in policy_commands.doctor_checks())


def test_status_reports_a_launcher_that_no_longer_exists(home, monkeypatch, tmp_path):
    from briefcase.cli import policy_commands
    gone = tmp_path / "old-venv" / "briefcase" / "policy" / "hook_main.py"
    monkeypatch.setattr(install, "launcher_path", lambda: str(gone))
    assert init(tools=["claude-code"])[0] == 0
    row = policy_commands.status()
    assert row["state"] == "attention" and "no longer exists" in row["detail"]


# --- the installed command, run for real ------------------------------------------------

def run_command(command, home, payload):
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", ""),
           "BRIEFCASE_SYSTEM_POLICY": os.environ["BRIEFCASE_SYSTEM_POLICY"]}
    return subprocess.run(shlex.split(command), input=json.dumps(payload).encode(), capture_output=True,
                          env=env, timeout=60)


PAYLOAD = {"session_id": "s", "cwd": "/", "hook_event_name": "PreToolUse", "tool_name": "Bash",
           "tool_input": {"command": "rm -rf /"}, "tool_use_id": "t"}


def test_installed_command_decides(home):
    (home / ".briefcase").mkdir()
    (home / ".briefcase" / "policy.toml").write_text('version = 1\npreset = "guarded"\n')
    for fail_closed in (False, True):
        command = install.hook_command("claude-code", install.HookOptions(fail_closed=fail_closed))
        done = run_command(command, home, PAYLOAD)
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_missing_launcher_fails_open_unless_fail_closed(home, monkeypatch, tmp_path):
    """Python exits 2 for a script file that does not exist, and exit code 2 blocks the call in
    Claude Code, Cursor and Codex. The default command form must therefore exit 1 (a failed hook,
    which every tool lets through); the fail-closed form keeps exit 2 so it blocks everywhere."""
    gone = str(tmp_path / "uninstalled" / "hook_main.py")
    monkeypatch.setattr(install, "launcher_path", lambda: gone)
    open_form = install.hook_command("claude-code", install.HookOptions())
    closed_form = install.hook_command("claude-code", install.HookOptions(fail_closed=True))
    assert run_command(open_form, home, PAYLOAD).returncode == 1
    assert run_command(closed_form, home, PAYLOAD).returncode == 2


def test_launcher_paths_are_found_in_both_command_forms(monkeypatch):
    monkeypatch.setattr(install, "launcher_path", lambda: "/opt/x/briefcase/policy/hook_main.py")
    for fail_closed in (False, True):
        parts = shlex.split(install.hook_command("codex", install.HookOptions(fail_closed=fail_closed)))
        assert install._launcher_paths(parts) == ["/opt/x/briefcase/policy/hook_main.py"]
    portable = shlex.split(install.hook_command("codex", install.HookOptions(portable=True)))
    assert portable[:3] == ["briefcase-ai", "hook", "codex"] and install._launcher_paths(portable) == []


def test_hook_command_flags():
    command = install.hook_command("cursor", install.HookOptions(
        portable=True, capture_content=True, endpoint="https://otel.example.com/"))
    assert command == "briefcase-ai hook cursor --enforce --capture-content --endpoint https://otel.example.com"
    assert install.marker("cursor") in command


def test_off_removes_only_the_hooks_the_person_confirmed(home, capsys, fake_bin, monkeypatch):
    """A hook installed for another tool while the question is open is not part of what was confirmed."""
    from briefcase.cli import policy_commands

    fake_bin("claude", "2.1.330 (Claude Code)")
    assert init(tools=["claude-code"])[0] == 0
    real = policy_commands._confirm

    def confirm_after_codex_lands(question, yes, **kwargs):
        if not question.startswith("Remove the policy hooks"):
            return real(question, yes, **kwargs)
        assert "claude-code" in question and "codex" not in question
        monkeypatch.setattr(policy_commands, "_confirm", real)
        assert init(tools=["codex"])[0] == 0  # lands while the prompt is open
        return True

    monkeypatch.setattr(policy_commands, "_confirm", confirm_after_codex_lands)
    code, text = off(capsys)
    assert code == 0, text
    assert "Enforcement is still ON for codex" in text and "Enforcement is off" not in text
    assert not files(home)["claude"].exists() or "enforce" not in files(home)["claude"].read_text()
    assert files(home)["codex"].exists() and "--enforce" in files(home)["codex"].read_text(), \
        "a hook that was not in the confirmation was removed"


def test_off_removes_only_the_files_it_showed_even_for_the_same_tool(home, capsys, fake_bin, monkeypatch):
    from briefcase.cli import policy_commands

    fake_bin("claude", "2.1.330 (Claude Code)")
    assert init(tools=["claude-code"])[0] == 0
    project_settings = home.parent / "project" / ".claude" / "settings.json"
    real = policy_commands._confirm

    def confirm_after_project_hook_lands(question, yes, **kwargs):
        if not question.startswith("Remove the policy hooks"):
            return real(question, yes, **kwargs)
        assert "settings.json" in question  # the question names the file it is about
        monkeypatch.setattr(policy_commands, "_confirm", real)
        assert init(tools=["claude-code"], scope="project")[0] == 0  # the same tool, another file
        return True

    monkeypatch.setattr(policy_commands, "_confirm", confirm_after_project_hook_lands)
    assert off(capsys)[0] == 0
    assert project_settings.exists() and "--enforce" in project_settings.read_text(), \
        "a hook in a file that was not shown was removed"
    assert "enforce" not in (files(home)["claude"].read_text() if files(home)["claude"].exists() else "")
