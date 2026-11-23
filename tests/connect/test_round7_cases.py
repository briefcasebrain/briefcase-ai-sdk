"""Each case the round-7 review reproduced, named, next to the generated invariants."""
import json
import os
import sys

import pytest

from briefcase.connect import edits, mcpreg

from tests.connect.test_invariants import SUPPLIED, World, seed

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX paths and modes")


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def test_a_refused_jsonc_snippet_never_prints_the_supplied_credential(w):
    path = w.telemetry_file("claude-code")
    path.parent.mkdir(parents=True)
    path.write_text('{\n  // my settings\n  "model": "opus"\n}\n')
    code, out, err = w.run(["connect", "claude-code", "--no-mcp", "--dry-run", "--headers",
                            "Authorization=Bearer " + SUPPLIED])
    assert SUPPLIED not in out + err and "***" in out


def test_a_credential_written_into_a_world_readable_file_leaves_it_0600(w):
    path = w.telemetry_file("claude-code")
    path.parent.mkdir(parents=True)
    path.write_text('{"model": "opus"}\n')
    path.chmod(0o644)
    flags = ["--headers", "Authorization=Bearer " + SUPPLIED]
    assert w.run(["connect", "claude-code", "--no-mcp", "--yes", *flags])[0] == 0
    assert SUPPLIED in path.read_text() and path.stat().st_mode & 0o777 == 0o600


def test_subprocess_failure_output_is_redacted(w):
    def runner(argv):
        if argv[2] == "get":
            return 1, 'No MCP server found with name: "briefcase"'
        return 1, "boom: could not use Authorization=Bearer " + SUPPLIED

    from briefcase.cli import connect_commands
    from briefcase.connect.adapters import registry

    from argparse import Namespace

    Args = Namespace(tools=["claude-code"], all=False, scope="user", endpoint=None, mcp=True, telemetry=True,
                     capture_content=False, connect_dry_run=False, dry_run=False, undo=False, yes=True, force=False,
                     start=False, headers=["Authorization=Bearer " + SUPPLIED])

    lines = []
    connect_commands.run_connect(Args, registry(), out=lines.append, offer_receiver=False, runner=runner)
    assert lines and SUPPLIED not in "\n".join(lines)


def test_two_connects_at_once_keep_both_ownership_records(tmp_path, monkeypatch):
    import multiprocessing

    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("needs fork")
    w = World(tmp_path, monkeypatch)
    ctx = multiprocessing.get_context("fork")
    real = edits.save_journal

    def slow_save(journal):  # widen the window in which an unlocked load-modify-save loses the other's record
        import time
        time.sleep(0.3)
        real(journal)

    monkeypatch.setattr(edits, "save_journal", slow_save)

    def run(tool):
        w.run(["connect", tool, "--yes", "--no-mcp"])

    procs = [ctx.Process(target=run, args=(t,)) for t in ("claude-code", "cursor")]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
    tools = {t["tool"] for t in edits.load_journal()["targets"].values()}
    assert tools == {"claude-code", "cursor"}, f"a record was lost: {tools}"


def test_a_policy_that_appears_between_preview_and_write_is_not_replaced(w, monkeypatch):
    from briefcase.cli import policy_commands

    policy = w.home / ".briefcase" / "policy.toml"
    assert w.run(["policy", "init", "claude-code", "--yes"])[0] == 0
    mine = 'version = 1\npreset = "strict"\n'

    def late_confirm(question, assume_yes, ask=None):
        policy.write_text(mine)  # the user saves their own policy while the preview is on screen
        return True

    monkeypatch.setattr(policy_commands, "_confirm", late_confirm)
    code, out, err = w.run(["policy", "init", "claude-code", "--force"])
    assert policy.read_text() == mine and code == 1


def test_a_hook_file_that_cannot_be_edited_is_not_reported_as_installed(w):
    path = w.telemetry_file("claude-code")
    path.parent.mkdir(parents=True)
    path.write_text('{\n  // comment\n  "model": "x"\n}\n')
    code, out, err = w.run(["policy", "init", "claude-code", "--yes"])
    assert code == 1 and "already installed" not in out


def test_a_claude_registration_that_moved_scope_is_not_ours(w):
    assert w.run(["mcp", "install", "claude-code", "--yes"])[0] == 0
    add = ["claude", "mcp", "add", "--scope", "user", "briefcase", "--", "uvx", "--from", "briefcase-ai[mcp]",
           "briefcase-ai-mcp"]
    text = ("briefcase:\n  Scope: Project config (shared via .mcp.json)\n  Status: ok\n  Type: stdio\n  Command: uvx\n"
            "  Args: --from briefcase-ai[mcp] briefcase-ai-mcp\n  Environment:\n")
    assert mcpreg.owned(add, lambda argv: (0, text)) == mcpreg.DIFFERS


def test_claude_arguments_are_compared_as_a_list_and_an_unreadable_line_is_unknown(w):
    add = ["claude", "mcp", "add", "--scope", "user", "briefcase", "--", "node", "a b", "c"]
    head = "briefcase:\n  Scope: User config (available in all your projects)\n  Type: stdio\n  Command: node\n"
    assert mcpreg.owned(add, lambda argv: (0, head + "  Args: a b c\n")) == mcpreg.UNKNOWN  # boundaries lost
    plain = ["claude", "mcp", "add", "--scope", "user", "briefcase", "--", "node", "a", "b c"[0:1]]
    assert mcpreg.owned(plain, lambda argv: (0, head + '  Args: a "b\n')) == mcpreg.UNKNOWN  # does not round-trip
    assert mcpreg.owned(plain, lambda argv: (0, head + "  Args: a b\n")) == mcpreg.OURS


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="XDG_CONFIG_HOME is a Linux convention")
def test_vscode_follows_xdg_config_home_or_the_lookup_is_unknown(w, monkeypatch, tmp_path):
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert w.vscode_settings() == xdg / "Code" / "User" / "settings.json"
    monkeypatch.setenv("VSCODE_PORTABLE", str(tmp_path / "portable"))
    assert mcpreg.lookup(["code", "--add-mcp", "{}"], lambda argv: (0, ""))[0] == mcpreg.UNKNOWN


def test_undo_does_not_delete_what_the_text_around_our_table_hides(w):
    seed(w, "codex", "toml_multiline")
    path = w.telemetry_file("codex")
    original = path.read_bytes()
    assert w.run(["connect", "codex", "--yes", "--no-mcp"])[0] == 0
    assert w.run(["connect", "--undo", "codex"])[0] == 0
    assert path.read_bytes() == original and json.dumps(edits._toml.loads(path.read_text()))


def test_the_command_lock_is_reentrant_within_one_command(w):
    from briefcase.connect.lock import command_lock

    with command_lock():
        with command_lock():
            assert os.path.exists(edits.journal_path().parent / ".command.lock")


def test_a_receiver_that_fails_to_start_is_reported(w, monkeypatch):
    from briefcase.cli import receive_commands

    monkeypatch.setattr(receive_commands, "start_detached", lambda args, out=print: 1)
    code, out, err = w.run(["connect", "claude-code", "--no-mcp", "--yes", "--start"])
    assert code == 1 and "did not start" in err


def test_status_does_not_call_a_deleted_registration_connected(w):
    assert w.run(["mcp", "install", "claude-code", "--yes"])[0] == 0
    code, out, _ = w.run(["connect", "status"])
    assert "DRIFTED" not in out
    w.cli.claude.clear()  # the user removed it by hand
    code, out, _ = w.run(["connect", "status"])
    assert "DRIFTED" in out and "MISSING" in out


def test_the_default_profile_undo_retry_says_to_unset_the_active_variable(w, monkeypatch, tmp_path):
    assert w.run(["mcp", "install", "claude-code", "--yes"])[0] == 0
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "other"))
    code, out, _ = w.run(["connect", "--undo", "claude-code", "--dry-run"])
    assert "env -u CLAUDE_CONFIG_DIR briefcase-ai connect --undo claude-code" in out


def test_a_journal_from_the_round_6_build_is_not_read(w):
    journal = edits.journal_path()
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps({"version": 2, "files": {}, "commands": [], "targets": {}}))
    code, out, err = w.run(["connect", "claude-code", "--no-mcp", "--yes"])
    assert code == 1 and "earlier build" in err
