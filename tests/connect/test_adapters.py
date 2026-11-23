"""Golden plans per adapter, in a temp HOME. Nothing here writes a file."""

import json
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

import pytest

from briefcase.connect import adapters as adapters_mod
from briefcase.connect import edits
from briefcase.connect.adapters import Context, load_data, registry, resolve_names, version_in_range
from briefcase.connect.edits import Command, FileEdit, Manual

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX paths")

UVX = {"command": "uvx", "args": ["--from", "briefcase-ai[mcp]", "briefcase-ai-mcp"]}


def plan(tool, **kwargs):
    return registry()[tool].plan(Context(**kwargs))


def kinds(items):
    return [type(item).__name__ for item in items]


def test_adapter_data_is_complete():
    data = load_data()
    assert data["schema_version"] == 1
    assert set(data["tools"]) == set(registry())
    for name, tool in data["tools"].items():
        for key in ("name", "mode", "detect", "docs_url", "verified_on", "unverified",
                    "min_tested", "max_tested", "supported"):
            assert key in tool, (name, key)
        assert tool["docs_url"].startswith("https://")
        assert tool["mode"] in ("automatic", "print-only", "experimental")


def test_nothing_is_detected_in_an_empty_home(home):
    assert [n for n, a in registry().items() if a.detect().installed] == []


def test_detection_by_config_dir_and_by_binary(home, fake_bin):
    (home / ".claude").mkdir()
    fake_bin("codex")
    found = {n: a.detect() for n, a in registry().items() if a.detect().installed}
    assert set(found) == {"claude-code", "codex"}
    assert "`codex` is on PATH" in found["codex"].reasons


def test_claude_code_golden_plan(home):
    items = plan("claude-code")
    assert kinds(items) == ["FileEdit", "Manual"]
    edit = items[0]
    assert edit.path == home / ".claude" / "settings.json" and edit.format == "json"
    assert {tuple(op["path"]): op["value"] for op in edit.ops} == {
        ("env", "CLAUDE_CODE_ENABLE_TELEMETRY"): "1",
        ("env", "OTEL_METRICS_EXPORTER"): "otlp",
        ("env", "OTEL_LOGS_EXPORTER"): "otlp",
        ("env", "OTEL_EXPORTER_OTLP_PROTOCOL"): "http/json",
        ("env", "OTEL_EXPORTER_OTLP_ENDPOINT"): "http://127.0.0.1:4318",
    }
    # `claude` is not on PATH: the MCP command is printed, not run.
    assert items[1].text == "claude mcp add --scope user briefcase -- uvx --from 'briefcase-ai[mcp]' briefcase-ai-mcp"


def test_claude_code_content_is_only_enabled_by_capture_content(home):
    default = {op["path"][-1] for op in plan("claude-code")[0].ops}
    assert not {"OTEL_LOG_USER_PROMPTS", "OTEL_LOG_TOOL_DETAILS"} & default
    captured = {op["path"][-1]: op["value"] for op in plan("claude-code", capture_content=True)[0].ops}
    assert captured["OTEL_LOG_USER_PROMPTS"] == "1" and captured["OTEL_LOG_TOOL_DETAILS"] == "1"


def test_claude_code_headers_and_endpoint(home):
    headers = {"Authorization": "Bearer t", "x": "1"}
    ops = plan("claude-code", endpoint="https://otel.example.com", headers=headers)[0].ops
    env = {op["path"][-1]: op["value"] for op in ops}
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://otel.example.com"
    assert env["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=Bearer t,x=1"


def test_claude_code_mcp_is_a_command_when_the_binary_exists(home, fake_bin, monkeypatch):
    monkeypatch.setattr(adapters_mod.mcp_payloads, "launcher_works", lambda: True)
    fake_bin("claude", "2.1.330 (Claude Code)")
    fake_bin("briefcase-ai-mcp")
    command = plan("claude-code")[1]
    assert isinstance(command, Command)
    assert command.argv == ["claude", "mcp", "add", "--scope", "user", "briefcase", "--", "briefcase-ai-mcp"]
    assert command.undo_hint == ["claude", "mcp", "remove", "--scope", "user", "briefcase"]


def test_claude_code_project_scope_writes_mcp_json_but_telemetry_stays_user(home, tmp_path):
    items = plan("claude-code", scope="project")
    assert kinds(items) == ["FileEdit", "FileEdit"]
    assert items[0].path == home / ".claude" / "settings.json"
    assert any("ignores telemetry variables in project settings" in note for note in items[0].notes)
    assert items[1].path == tmp_path / "project" / ".mcp.json"
    assert items[1].ops == [{"op": "set", "path": ["mcpServers", "briefcase"], "value": UVX}]


def test_claude_code_reports_managed_settings_overrides(home, tmp_path):
    managed = tmp_path / "managed-settings.json"
    managed.write_text(json.dumps({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://corp"}}))
    adapter = registry()["claude-code"]
    adapter.data["managed_settings"] = [str(managed)]
    adapter.data["managed_settings_dirs"] = []
    items = adapter.plan(Context(mcp=False))
    assert kinds(items) == ["FileEdit", "Manual"]
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in items[1].reason and "never writes managed files" in items[1].reason
    assert managed.read_text() == json.dumps({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://corp"}})


def test_unsupported_tool_version_falls_back_to_print_only(home, fake_bin):
    fake_bin("claude", "9.0.0 (Claude Code)")
    items = plan("claude-code")
    assert kinds(items) == ["Manual", "Manual"]
    assert "outside the range" in items[0].reason and "CLAUDE_CODE_ENABLE_TELEMETRY" in items[0].text
    assert kinds(plan("claude-code", force=True)) == ["FileEdit", "Command"]


def test_version_range_logic():
    supported = {"min": "2.0.0", "below": "3.0.0"}
    assert version_in_range("2.1.330", supported) is True
    assert version_in_range("3.0.0", supported) is False
    assert version_in_range("1.9", supported) is False
    assert version_in_range(None, supported) is None
    assert version_in_range("2.1.0", None) is None


def test_codex_golden_plan(home):
    (edit,) = plan("codex")
    assert edit.path == home / ".codex" / "config.toml" and edit.format == "toml"
    assert edit.toml_tables == ("otel", "mcp_servers.briefcase")
    parsed = tomllib.loads(edit.toml_body)
    assert parsed == {
        "otel": {
            "environment": "dev",
            "exporter": {"otlp-http": {
                "endpoint": "http://127.0.0.1:4318/v1/logs", "protocol": "binary", "headers": {}}},
            "log_user_prompt": False,
        },
        "mcp_servers": {"briefcase": UVX},
    }
    assert any("briefcase-ai[receiver]" in note for note in edit.notes)


def test_codex_capture_content_headers_and_mcp_command(home, fake_bin):
    fake_bin("codex", "codex-cli 0.50.0")
    edit, command = plan("codex", capture_content=True, headers={"Authorization": "Bearer t"})
    parsed = tomllib.loads(edit.toml_body)
    assert parsed["otel"]["log_user_prompt"] is True
    assert parsed["otel"]["exporter"]["otlp-http"]["headers"] == {"Authorization": "Bearer t"}
    assert "mcp_servers" not in parsed and edit.toml_tables == ("otel",)
    assert command.argv == ["codex", "mcp", "add", "briefcase", "--", *UVX["command"].split(), *UVX["args"]]


def test_cursor_golden_plan(home, fake_bin):
    fake_bin("briefcase-ai")
    hooks, mcp = plan("cursor")
    assert hooks.path == home / ".cursor" / "hooks.json"
    entry = {"command": "briefcase-ai hook cursor", "timeout": 5}
    assert hooks.ops[0] == {"op": "default", "path": ["version"], "value": 1}
    assert [(op["op"], op["path"], op["value"]) for op in hooks.ops[1:]] == [
        ("append", ["hooks", event], entry)
        for event in ("sessionStart", "sessionEnd", "beforeSubmitPrompt", "postToolUse", "afterAgentResponse", "stop")
    ]
    assert mcp.path == home / ".cursor" / "mcp.json"
    assert mcp.ops == [{"op": "set", "path": ["mcpServers", "briefcase"], "value": {"type": "stdio", **UVX}}]


def test_cursor_project_scope_and_flags(home, tmp_path):
    hooks, mcp = plan("cursor", scope="project", endpoint="http://127.0.0.1:9999", capture_content=True)
    assert hooks.path == tmp_path / "project" / ".cursor" / "hooks.json"
    assert mcp.path == tmp_path / "project" / ".cursor" / "mcp.json"
    command = hooks.ops[1]["value"]["command"]
    assert command.endswith("-m briefcase.cli hook cursor --endpoint http://127.0.0.1:9999 --capture-content")


def test_vscode_copilot_golden_plan(home):
    edit, manual = plan("vscode-copilot")
    assert edit.path.parts[-3:] == ("Code", "User", "settings.json") and home in edit.path.parents
    assert {op["path"][0]: op["value"] for op in edit.ops} == {
        "github.copilot.chat.otel.enabled": True,
        "github.copilot.chat.otel.exporterType": "otlp-http",
        "github.copilot.chat.otel.otlpEndpoint": "http://127.0.0.1:4318",
    }
    assert isinstance(manual, Manual) and manual.text.startswith("code --add-mcp ")
    payload = json.loads(manual.text.split(" ", 2)[2].strip("'"))
    assert payload == {"name": "briefcase", "type": "stdio", **UVX}


def test_vscode_copilot_capture_and_project_mcp(home, tmp_path):
    edit, mcp = plan("vscode-copilot", capture_content=True, scope="project")
    assert {"path": ["github.copilot.chat.otel.captureContent"], "op": "set", "value": True} in edit.ops
    assert mcp.path == tmp_path / "project" / ".vscode" / "mcp.json"
    assert mcp.ops[0]["path"] == ["servers", "briefcase"]


def test_gemini_is_print_only_until_the_user_file_exists(home, tmp_path):
    (manual,) = plan("gemini-cli")
    assert isinstance(manual, Manual) and '"logPrompts": false' in manual.text
    (home / ".gemini").mkdir()
    (home / ".gemini" / "settings.json").write_text("{}")
    (edit,) = plan("gemini-cli")
    assert isinstance(edit, FileEdit) and edit.path == home / ".gemini" / "settings.json"
    assert {op["path"][1]: op["value"] for op in edit.ops} == {
        "enabled": True, "target": "local", "otlpEndpoint": "http://127.0.0.1:4318",
        "otlpProtocol": "http", "logPrompts": False,
    }
    (project,) = plan("gemini-cli", scope="project")
    assert project.path == tmp_path / "project" / ".gemini" / "settings.json"


@pytest.mark.parametrize("tool", ["cowork", "copilot-cli", "cline", "windsurf", "opencode"])
def test_print_only_tools_never_plan_a_write(home, tool):
    for kwargs in ({}, {"scope": "project"}, {"capture_content": True}, {"force": True}):
        items = plan(tool, **kwargs)
        assert items and all(isinstance(item, Manual) for item in items), (tool, kwargs)


def test_cowork_prints_admin_steps_with_the_endpoint(home):
    (manual,) = plan("cowork", endpoint="https://otel.example.com")
    assert "Organization settings > Data and privacy > Monitoring" in manual.text
    assert "https://otel.example.com" in manual.text


def test_no_mcp_and_no_telemetry_switches(home):
    assert kinds(plan("claude-code", mcp=False)) == ["FileEdit"]
    assert kinds(plan("cursor", mcp=False)) == ["FileEdit"]
    assert kinds(plan("cursor", telemetry=False)) == ["FileEdit"]
    assert plan("cursor", telemetry=False)[0].path.name == "mcp.json"
    assert plan("cowork", telemetry=False) == []


def test_resolve_names_aliases_and_unknown():
    assert resolve_names(["claude", "Codex", "vscode", "claude-code"]) == ["claude-code", "codex", "vscode-copilot"]
    with pytest.raises(KeyError):
        resolve_names(["emacs"])


def test_version_is_read_through_the_binary(home, fake_bin, monkeypatch):
    fake_bin("claude", "2.1.330 (Claude Code)")
    assert registry()["claude-code"].version() == "2.1.330"
    monkeypatch.setattr(adapters_mod, "run_version", lambda argv: None)
    assert registry()["claude-code"].version() is None


def test_every_planned_file_edit_resolves_cleanly_on_an_empty_home(home):
    for name, adapter in registry().items():
        for item in adapter.plan(Context()):
            if isinstance(item, FileEdit):
                resolved = edits.resolve(item)
                assert resolved.status == edits.CHANGE, (name, resolved.reason)
                assert not item.path.exists()
