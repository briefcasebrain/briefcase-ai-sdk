"""Each case the round-9 review reproduced, named, next to the generated invariants."""
import json
import os
import sys

import pytest

from briefcase.connect import adapters, edits, engine, mcpreg

from tests.connect.test_invariants import SUPPLIED, World

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX paths and modes")

ENDPOINT = ["--endpoint", "https://new.example.com"]


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def connect_claude(w):
    return w.run(["connect", "claude-code", "--no-mcp", "--yes", *ENDPOINT])


def wrote_endpoint(w):
    path = w.telemetry_file("claude-code")
    return path.exists() and "new.example.com" in path.read_text()


# --- the capture allowlist covers the process environment and every other layer -------------------------------------

@pytest.mark.parametrize("key", ["OTEL_LOG_RAW_API_BODIES", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
                                 "OTEL_EXPORTER_OTLP_HEADERS", "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"])
def test_a_variable_already_exported_blocks_a_first_connect(w, monkeypatch, key):
    monkeypatch.setenv(key, "SECRET-VALUE-1")
    code, out, err = connect_claude(w)
    assert code == 1 and key in out + err and "SECRET-VALUE-1" not in out + err
    assert not wrote_endpoint(w)


def test_a_variable_we_write_ourselves_does_not_block(w, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    assert connect_claude(w)[0] == 0 and wrote_endpoint(w)


def test_managed_settings_from_the_registry_or_a_plist_block_a_first_connect(w, monkeypatch):
    monkeypatch.setattr(adapters, "_registry_settings",
                        lambda hive="": json.dumps({"env": {"OTEL_LOG_RAW_API_BODIES": "1"}}))
    code, out, err = connect_claude(w)
    assert code == 1 and "OTEL_LOG_RAW_API_BODIES" in out + err and "HKLM" in out + err
    monkeypatch.setattr(adapters, "_registry_settings", lambda hive="": None)
    monkeypatch.setattr(adapters, "_managed_preferences",
                        lambda: json.dumps({"env": {"OTEL_EXPORTER_OTLP_LOGS_HEADERS": "x"}}))
    code, out, err = connect_claude(w)
    assert code == 1 and "OTEL_EXPORTER_OTLP_LOGS_HEADERS" in out + err and "managed preferences" in out + err
    assert not wrote_endpoint(w)


def test_the_windows_managed_settings_locations_are_checked(w):
    data = adapters.load_data()["tools"]["claude-code"]
    assert any("ClaudeCode" in p and p.lower().endswith("managed-settings.json") and p[1:3] == ":\\"
               for p in data["managed_settings"])
    assert any(p.lower().endswith("managed-settings.d") and p[1:3] == ":\\" for p in data["managed_settings_dirs"])


@pytest.mark.parametrize("shape", ["invalid_json", "not_an_object", "unreadable", "registry_unreadable"])
def test_a_layer_that_cannot_be_read_or_understood_counts_as_a_risk(w, monkeypatch, shape):
    path = w.project / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if shape == "invalid_json":
        path.write_text("{not json")
    elif shape == "not_an_object":
        path.write_text("[1, 2]")
    elif shape == "unreadable":
        if os.geteuid() == 0:
            pytest.skip("root reads everything")
        path.write_text("{}")
        path.chmod(0)
    else:
        def broken(hive=""):
            raise OSError("access denied")

        monkeypatch.setattr(adapters, "_registry_settings", broken)
    try:
        code, out, err = connect_claude(w)
    finally:
        if path.exists():
            path.chmod(0o600)
    assert code == 1 and not wrote_endpoint(w), (out, err)
    assert "cannot be read" in out + err or "unknown" in out + err


def test_a_layer_that_is_simply_absent_is_not_a_risk(w):
    assert connect_claude(w)[0] == 0 and wrote_endpoint(w)


# --- a registration whose argument boundaries cannot be proven is unknown, in every scope ---------------------------

LISTING = ("briefcase:\n  Scope: {scope}\n  Type: stdio\n  Command: uvx\n  Args: --from a b\n  Environment:\n")
SCOPES = {"user": "User config (available in all your projects)",
          "local": "Local config (private to you in this project)",
          "project": "Project config (shared via .mcp.json)"}


def claude_files(w, scope, args):
    entry = {"command": "uvx", "args": args}
    if scope == "user":
        (w.home / ".claude.json").write_text(json.dumps({"mcpServers": {"briefcase": entry}}))
    elif scope == "local":
        (w.home / ".claude.json").write_text(json.dumps({"projects": {os.getcwd(): {"mcpServers": {
            "briefcase": entry}}}}))
    else:
        (w.project / ".mcp.json").write_text(json.dumps({"mcpServers": {"briefcase": entry}}))


@pytest.mark.parametrize("scope", list(SCOPES))
def test_without_claudes_own_file_no_scope_can_prove_ownership(w, scope):
    add = ["claude", "mcp", "add", "--scope", scope, "briefcase", "--", "uvx", "--from", "a", "b"]
    text = LISTING.format(scope=SCOPES[scope])
    assert mcpreg.owned(add, lambda argv: (0, text)) == mcpreg.UNKNOWN
    claude_files(w, scope, ["--from", "a", "b"])
    assert mcpreg.owned(add, lambda argv: (0, text)) == mcpreg.OURS
    claude_files(w, scope, ["--from", "a b"])  # the same flattened line, different boundaries
    assert mcpreg.owned(add, lambda argv: (0, text)) == mcpreg.DIFFERS


def test_undo_never_recommends_removing_a_registration_it_cannot_prove_is_ours(w):
    assert w.run(["mcp", "install", "claude-code", "--yes"])[0] == 0
    (w.home / ".claude.json").unlink()
    w.cli._claude_file = lambda profile, scope: None  # Claude's file is gone and stays gone
    code, out, err = w.run(["connect", "--undo", "claude-code"])
    assert "to remove the MCP registration" not in out + err and "claude mcp remove" not in out + err
    assert "not undone" in out
    assert [c for c in edits.load_journal()["commands"] if c["tool"] == "claude-code"], "the record must be kept"


# --- the registration is journalled before the add can return --------------------------------------------------------

def test_an_interrupt_right_after_a_successful_add_leaves_the_registration_recorded(w, monkeypatch):
    real = w.cli.__call__

    def add_then_die(argv):
        result = real(argv)
        if argv[:3] == ["claude", "mcp", "add"]:
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(engine, "run_command", add_then_die)
    assert w.run(["mcp", "install", "claude-code", "--yes"])[0] == 130
    assert w.cli.claude, "the add took effect"
    journal = edits.load_journal()
    assert [c["argv"][:3] for c in journal["commands"]] == [["claude", "mcp", "add"]], "no record names the add"
    monkeypatch.setattr(engine, "run_command", real)
    code, out, err = w.run(["connect", "--undo", "claude-code"])
    assert "claude mcp remove" in out  # and undo can now say how to take it back


def test_a_failed_add_leaves_no_record(w, monkeypatch):
    monkeypatch.setattr(engine, "run_command", lambda argv: (1, 'No MCP server found with name: "briefcase"')
                        if argv[2] == "get" else (1, "boom"))
    w.run(["mcp", "install", "claude-code", "--yes"])
    journal = edits.load_journal()
    assert journal["commands"] == [] and not [t for t in journal["targets"].values() if t["kind"] == "mcp"]


# --- credentials never reach a retry hint or a log, in any form argparse accepts -------------------------------------

@pytest.mark.parametrize("argv", [
    ["connect", "claude-code", "--head", "Authorization=Basic " + SUPPLIED],
    ["connect", "claude-code", "--header=Authorization=Basic " + SUPPLIED],
    ["connect", "claude-code", "--headers", "A=" + SUPPLIED],
    ["connect", "claude-code", "--tok", SUPPLIED],
    ["secret", "set", "KEY=" + SUPPLIED],
    ["--server", "h:1", "connect", "claude-code", "--headers=A=" + SUPPLIED],
])
def test_no_argv_form_puts_a_credential_in_the_retry_hint(argv, capsys):
    from briefcase.cli import __main__ as cli

    def boom():
        raise RuntimeError("something broke")

    assert cli._run(boom, argv) == 1
    err = capsys.readouterr().err
    assert SUPPLIED not in err and "Fix:" in err


def test_the_retry_hint_for_a_plain_command_is_unchanged(capsys):
    from briefcase.cli import __main__ as cli

    cli._run(lambda: (_ for _ in ()).throw(RuntimeError("something broke")), ["features"])
    assert "Fix: briefcase-ai --debug features   # shows the full traceback" in capsys.readouterr().err


@pytest.mark.parametrize("text", [
    "Authorization: Basic dXNlcjpwYXNzd29yZA==", '"Authorization": "Basic dXNlcjpwYXNzd29yZA=="',
    "authorization=Basic dXNlcjpwYXNzd29yZA==", "got Basic dXNlcjpwYXNzd29yZA== back",
])
def test_a_basic_credential_is_masked_not_just_the_word_basic(text):
    out = edits.redact(text, [])
    assert "dXNlcjpwYXNzd29yZA" not in out and "Basic" in out


def test_ordinary_words_after_basic_are_not_masked():
    assert edits.redact("a basic installation of the tool", []) == "a basic installation of the tool"


# --- round 10 ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["ENABLE_BETA_TRACING_DETAILED", "BETA_TRACING_ENDPOINT"])
def test_claudes_beta_tracing_pair_blocks_a_first_connect_in_the_environment_and_in_settings(w, monkeypatch, key):
    monkeypatch.setenv(key, "https://elsewhere.example.com")
    code, out, err = connect_claude(w)
    assert code == 1 and key in out + err and not wrote_endpoint(w)
    monkeypatch.delenv(key)
    path = w.telemetry_file("claude-code")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": {key: "1"}}))
    code, out, err = connect_claude(w)
    assert code == 1 and key in out + err


def test_the_windows_user_registry_value_is_a_layer(w, monkeypatch):
    seen = []

    def registry(hive="HKEY_LOCAL_MACHINE"):
        seen.append(hive)
        return json.dumps({"env": {"OTEL_LOG_RAW_API_BODIES": "1"}}) if hive == "HKEY_CURRENT_USER" else None

    monkeypatch.setattr(adapters, "_registry_settings", registry)
    code, out, err = connect_claude(w)
    assert code == 1 and "HKCU" in out + err and "HKEY_CURRENT_USER" in seen


def test_a_managed_directory_that_cannot_be_listed_is_a_risk_not_an_empty_layer(w, monkeypatch, tmp_path):
    managed = tmp_path / "managed.d"
    managed.mkdir()
    data = adapters.load_data()
    data["tools"]["claude-code"]["managed_settings_dirs"] = [str(managed)]
    monkeypatch.setattr(adapters, "load_data", lambda: data)
    real = os.listdir

    def listdir(path="."):
        if str(path) == str(managed):
            raise PermissionError(13, "denied")  # the same condition on every platform
        return real(path)

    monkeypatch.setattr(adapters.os, "listdir", listdir)
    code, out, err = connect_claude(w)
    assert code == 1 and "managed settings directory" in out + err and not wrote_endpoint(w)


def test_a_managed_drop_in_added_after_the_preview_is_a_layer_at_write_time(w, monkeypatch, tmp_path):
    from briefcase.connect.adapters import Context

    managed = tmp_path / "managed.d"
    managed.mkdir()
    data = adapters.load_data()
    data["tools"]["claude-code"]["managed_settings_dirs"] = [str(managed)]
    monkeypatch.setattr(adapters, "load_data", lambda: data)
    ctx = Context(endpoint="https://new.example.com", mcp=False, project_dir=w.project)
    plan = engine.build_plan(["claude-code"], ctx, runner=w.cli)
    assert not plan.blocked
    (managed / "50-new.json").write_text(json.dumps({"env": {"OTEL_LOG_RAW_API_BODIES": "1"}}))  # a new member
    out = []
    assert engine.apply_plan(plan, ctx, out.append, runner=w.cli) == 1
    assert "OTEL_LOG_RAW_API_BODIES" in "\n".join(out) and not wrote_endpoint(w)


def test_a_layer_that_changes_between_the_preview_and_the_write_is_rechecked(w):
    from briefcase.connect.adapters import Context

    ctx = Context(endpoint="https://new.example.com", mcp=False, project_dir=w.project)
    plan = engine.build_plan(["claude-code"], ctx, runner=w.cli)
    assert not plan.blocked  # the preview was clean ...
    path = w.project / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": {"OTEL_LOG_RAW_API_BODIES": "1"}}))  # ... and a layer lands after "yes"
    out = []
    assert engine.apply_plan(plan, ctx, out.append, runner=w.cli) == 1
    assert "OTEL_LOG_RAW_API_BODIES" in "\n".join(out) and not wrote_endpoint(w)


def test_a_timeout_after_the_add_committed_keeps_the_record(w, monkeypatch):
    real = w.cli.__call__

    def commit_then_time_out(argv):
        result = real(argv)
        return (1, "timed out") if argv[:3] == ["claude", "mcp", "add"] else result

    monkeypatch.setattr(engine, "run_command", commit_then_time_out)
    w.run(["mcp", "install", "claude-code", "--yes"])
    assert w.cli.claude, "the add took effect"
    assert [c["argv"][:3] for c in edits.load_journal()["commands"]] == [["claude", "mcp", "add"]]
