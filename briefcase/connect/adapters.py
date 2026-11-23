"""Per-tool adapters for ``briefcase-ai connect``.

Each adapter knows how to ``detect()`` its tool, build a ``plan()`` (a list of
:class:`FileEdit`, :class:`Command` and :class:`Manual` items) and report its
installed version. Paths, keys and documentation provenance live in
``adapters.json`` next to this file, so a format change is a data change.

Anything a tool's documentation does not pin down is a :class:`Manual` item:
it is printed for the user and never written.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from briefcase.connect import mcp as mcp_payloads
from briefcase.connect.edits import Command, FileEdit, Manual, home, snippet

DEFAULT_ENDPOINT = "http://127.0.0.1:4318"
HOOK_MARKER = "hook cursor"

_DATA_FILE = Path(__file__).with_name("adapters.json")


def load_data() -> Dict[str, Any]:
    return json.loads(_DATA_FILE.read_text(encoding="utf-8"))


@dataclass
class Context:
    scope: str = "user"
    endpoint: str = DEFAULT_ENDPOINT
    headers: Dict[str, str] = field(default_factory=dict)
    mcp: bool = True
    telemetry: bool = True
    capture_content: bool = False
    force: bool = False
    project_dir: Path = field(default_factory=Path.cwd)


@dataclass
class Detection:
    installed: bool
    reasons: List[str]
    version: Optional[str] = None


def _expand(raw: Any) -> Path:
    if isinstance(raw, dict):
        key = "win32" if sys.platform.startswith("win") else (
            "darwin" if sys.platform == "darwin" else "linux")
        raw = raw[key]
    text = str(raw)
    if "%APPDATA%" in text:
        text = text.replace("%APPDATA%", os.environ.get("APPDATA", str(home() / "AppData" / "Roaming")))
    if text.startswith("~"):
        return home() / text[2:]
    return Path(text)


def which(name: str) -> Optional[str]:
    return shutil.which(name)


def run_version(argv: List[str]) -> Optional[str]:
    """Output of ``<tool> --version``; ``None`` when it cannot be run."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return (done.stdout or done.stderr or "").strip() or None


def parse_version(text: Optional[str]) -> Optional[Tuple[int, ...]]:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not match:
        return None
    return tuple(int(g or 0) for g in match.groups())


def version_in_range(version: Optional[str], supported: Optional[Dict[str, str]]) -> Optional[bool]:
    """``True``/``False`` when both are known, ``None`` when there is nothing to compare."""
    parsed = parse_version(version)
    if parsed is None or not supported:
        return None
    low, high = parse_version(supported.get("min")), parse_version(supported.get("below"))
    if low is not None and parsed < low:
        return False
    if high is not None and parsed >= high:
        return False
    return True


class Adapter:
    id = ""

    def __init__(self, data: Dict[str, Any]) -> None:
        self.data = data
        self.name: str = data["name"]
        self.mode: str = data["mode"]
        self.docs_url: str = data["docs_url"]
        self.verified_on: str = data["verified_on"]
        self.needs_protobuf: bool = bool(data.get("needs_protobuf"))

    # -- discovery -------------------------------------------------------------
    def path(self, key: str) -> Path:
        return _expand(self.data["files"][key])

    def binary(self) -> Optional[str]:
        name = self.data.get("bin")
        return which(name) if name else None

    def version(self) -> Optional[str]:
        binary = self.binary()
        if not binary:
            return None
        parsed = parse_version(run_version([binary, "--version"]))
        return ".".join(str(p) for p in parsed) if parsed else None

    def detect(self) -> Detection:
        spec = self.data.get("detect", {})
        reasons = []
        for raw in spec.get("dirs", []):
            path = _expand(raw)
            if path.is_dir():
                reasons.append(f"{path} exists")
        for raw in spec.get("globs", []):
            if glob.glob(str(_expand(raw))):
                reasons.append(f"{raw} matches")
        for name in spec.get("bins", []):
            if which(name):
                reasons.append(f"`{name}` is on PATH")
        return Detection(bool(reasons), reasons)

    # -- planning --------------------------------------------------------------
    def plan(self, ctx: Context) -> List[Any]:
        items = self._plan(ctx)
        if not any(isinstance(i, (FileEdit, Command)) for i in items):
            return items
        version = self.version()
        if version_in_range(version, self.data.get("supported")) is False and not ctx.force:
            supported = self.data["supported"]
            reason = (
                f"{self.name} {version} is outside the range this adapter was written for "
                f"(>= {supported.get('min')}, < {supported.get('below')}); printing instead of "
                "writing. Re-run with --force to write anyway."
            )
            return [self.to_manual(item, reason) for item in items]
        return items

    def _plan(self, ctx: Context) -> List[Any]:
        raise NotImplementedError

    def to_manual(self, item: Any, reason: str) -> Manual:
        if isinstance(item, FileEdit):
            return Manual(self.id, f"Add to {item.path}", snippet(item), reason)
        if isinstance(item, Command):
            return Manual(self.id, item.purpose, mcp_payloads.shell(item.argv), reason)
        return item

    def env_exports(self, env: Dict[str, str]) -> str:
        return "\n".join(f"export {key}={shlex.quote(value)}" for key, value in env.items())

    def _mcp_command_or_manual(self, argv: List[str], undo: Optional[List[str]]) -> Any:
        purpose = f"Register the briefcase MCP server with {self.name}"
        if which(argv[0]):
            return Command(self.id, argv, purpose, undo_hint=undo)
        return Manual(self.id, purpose, mcp_payloads.shell(argv),
                      f"`{argv[0]}` is not on PATH, so run this yourself once it is")


def _headers_csv(headers: Dict[str, str]) -> str:
    return ",".join(f"{key}={value}" for key, value in headers.items())


class ClaudeCode(Adapter):
    id = "claude-code"

    def managed_overrides(self, keys: List[str]) -> List[Tuple[str, List[str]]]:
        """Managed settings files that define any of ``keys`` under ``env``;
        those win over the user file, so connect and doctor report them."""
        found = []
        paths = [Path(p) for p in self.data.get("managed_settings", [])]
        for directory in self.data.get("managed_settings_dirs", []):
            paths.extend(sorted(Path(directory).glob("*.json")) if Path(directory).is_dir() else [])
        for path in paths:
            try:
                env = json.loads(path.read_text(encoding="utf-8")).get("env", {})
            except (OSError, ValueError, AttributeError):
                continue
            hit = [key for key in keys if isinstance(env, dict) and key in env]
            if hit:
                found.append((str(path), hit))
        return found

    def telemetry_env(self, ctx: Context) -> Dict[str, str]:
        env = {
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "OTEL_METRICS_EXPORTER": "otlp",
            "OTEL_LOGS_EXPORTER": "otlp",
            # http/json so the receiver works on a bare install, with no
            # protobuf dependency.
            "OTEL_EXPORTER_OTLP_PROTOCOL": self.data["otlp_protocol"],
            "OTEL_EXPORTER_OTLP_ENDPOINT": ctx.endpoint,
        }
        if ctx.headers:
            env["OTEL_EXPORTER_OTLP_HEADERS"] = _headers_csv(ctx.headers)
        if ctx.capture_content:
            env["OTEL_LOG_USER_PROMPTS"] = "1"
            env["OTEL_LOG_TOOL_DETAILS"] = "1"
        return env

    def _plan(self, ctx: Context) -> List[Any]:
        items: List[Any] = []
        if ctx.telemetry:
            env = self.telemetry_env(ctx)
            notes = []
            if ctx.scope == "project":
                notes.append("Claude Code ignores telemetry variables in project settings, "
                             "so telemetry is written to your user settings")
            items.append(FileEdit(
                self.id, self.path("user_settings"), "json",
                "Turn on Claude Code's OpenTelemetry export and point it at the briefcase receiver",
                ops=[{"op": "set", "path": ["env", key], "value": value} for key, value in env.items()],
                notes=notes,
            ))
            for path, keys in self.managed_overrides(list(env)):
                items.append(Manual(
                    self.id, f"Managed settings override: {path}",
                    json.dumps({"env": env}, indent=2),
                    f"{path} sets {', '.join(keys)}, and managed settings win over your user "
                    "settings. briefcase never writes managed files; ask your administrator to "
                    "apply this snippet there",
                ))
        if ctx.mcp:
            config = mcp_payloads.server_config(which)
            if ctx.scope == "project":
                items.append(FileEdit(
                    self.id, ctx.project_dir / self.data["files"]["project_mcp"], "json",
                    "Register the briefcase MCP server for this project",
                    ops=[{"op": "set", "path": ["mcpServers", mcp_payloads.SERVER_NAME], "value": config}],
                ))
            else:
                items.append(self._mcp_command_or_manual(
                    mcp_payloads.claude_add_argv(config, "user"), mcp_payloads.claude_remove_argv("user")))
        return items


class Cowork(Adapter):
    id = "cowork"

    def _plan(self, ctx: Context) -> List[Any]:
        if not ctx.telemetry:
            return []
        text = "\n".join([
            "An organization admin has to turn this on; nothing on this machine can.",
            "  1. Open Organization settings > Data and privacy > Monitoring.",
            "  2. Enable OpenTelemetry export.",
            f"  3. Set the OTLP endpoint to a receiver your organization runs, e.g. {ctx.endpoint}",
            "     (run `briefcase-ai receive --host 0.0.0.0 --token <secret>` behind TLS for a shared one,",
            "     and send the token as an `Authorization=Bearer <secret>` header).",
            "  4. If the exporter sends protobuf: pip install 'briefcase-ai[receiver]' on the receiver.",
            "Cowork always sends user.email and workspace.host_paths; the receiver hashes the",
            "email and drops the paths unless it is started with --keep-content.",
        ])
        return [Manual(self.id, "Claude Desktop / Cowork telemetry is admin-configured", text,
                       "there is no local setting to write")]


class Codex(Adapter):
    id = "codex"

    def otel_toml(self, ctx: Context) -> str:
        headers = "{ " + ", ".join(
            f"{json.dumps(k)} = {json.dumps(v)}" for k, v in ctx.headers.items()) + " }" if ctx.headers else "{}"
        endpoint = ctx.endpoint.rstrip("/") + "/v1/logs"
        return "\n".join([
            "[otel]",
            'environment = "dev"',
            "exporter = { otlp-http = { endpoint = " + json.dumps(endpoint)
            + f', protocol = "{self.data["otlp_protocol"]}", headers = {headers} }} }}',
            f"log_user_prompt = {'true' if ctx.capture_content else 'false'}",
        ])

    def _plan(self, ctx: Context) -> List[Any]:
        items: List[Any] = []
        body: List[str] = []
        tables: List[str] = []
        notes: List[str] = []
        command = None
        if ctx.telemetry:
            body.append(self.otel_toml(ctx))
            tables.append("otel")
            notes.append("Codex sends protobuf: the receiver needs `pip install 'briefcase-ai[receiver]'`")
            if ctx.scope == "project":
                notes.append("Codex ignores [otel] in project config, so this goes in your user config")
        if ctx.mcp:
            config = mcp_payloads.server_config(which)
            if which("codex"):
                command = Command(self.id, mcp_payloads.codex_add_argv(config),
                                  f"Register the briefcase MCP server with {self.name}",
                                  undo_hint=mcp_payloads.codex_remove_argv())
            else:
                body.append(mcp_payloads.codex_toml(config))
                tables.append(f"mcp_servers.{mcp_payloads.SERVER_NAME}")
        if body:
            items.append(FileEdit(
                self.id, self.path("user_config"), "toml",
                "Point Codex's OpenTelemetry export at the briefcase receiver" if ctx.telemetry
                else "Register the briefcase MCP server with Codex",
                toml_body="\n\n".join(body), toml_tables=tuple(tables), notes=notes,
            ))
        if command is not None:
            items.append(command)
        return items


class Cursor(Adapter):
    id = "cursor"

    def hook_command(self, ctx: Context) -> str:
        if which("briefcase-ai"):
            base = "briefcase-ai hook cursor"
        else:
            base = f"{shlex.quote(sys.executable)} -m briefcase.cli hook cursor"
        if ctx.endpoint != DEFAULT_ENDPOINT:
            base += f" --endpoint {shlex.quote(ctx.endpoint)}"
        if ctx.capture_content:
            base += " --capture-content"
        return base

    def _plan(self, ctx: Context) -> List[Any]:
        items: List[Any] = []
        project = ctx.scope == "project"
        if ctx.telemetry:
            path = ctx.project_dir / self.data["files"]["project_hooks"] if project else self.path("user_hooks")
            entry = {"command": self.hook_command(ctx), "timeout": self.data["hook_timeout_seconds"]}
            ops: List[Dict[str, Any]] = [{"op": "default", "path": ["version"], "value": 1}]
            ops += [{"op": "append", "path": ["hooks", event], "value": entry, "owned_by": HOOK_MARKER}
                    for event in self.data["hook_events"]]
            notes = ["Cursor has no native OpenTelemetry export; these hooks forward hook events "
                     "to the receiver and always exit 0, so they can never block the agent"]
            if ctx.headers:
                notes.append("--headers is not used by the Cursor hook; set BRIEFCASE_RECEIVER_TOKEN "
                             "in Cursor's environment if the receiver needs a token")
            items.append(FileEdit(self.id, path, "json", "Forward Cursor agent hook events to the briefcase receiver",
                                  ops=ops, notes=notes))
        if ctx.mcp:
            path = ctx.project_dir / self.data["files"]["project_mcp"] if project else self.path("user_mcp")
            entry = mcp_payloads.stdio_entry(mcp_payloads.server_config(which))
            items.append(FileEdit(
                self.id, path, "json", "Register the briefcase MCP server with Cursor",
                ops=[{"op": "set", "path": ["mcpServers", mcp_payloads.SERVER_NAME], "value": entry}],
            ))
        return items


class VSCodeCopilot(Adapter):
    id = "vscode-copilot"

    def settings(self, ctx: Context) -> Dict[str, Any]:
        values: Dict[str, Any] = {
            "github.copilot.chat.otel.enabled": True,
            "github.copilot.chat.otel.exporterType": "otlp-http",
            "github.copilot.chat.otel.otlpEndpoint": ctx.endpoint,
        }
        if ctx.capture_content:
            values["github.copilot.chat.otel.captureContent"] = True
        return values

    def _plan(self, ctx: Context) -> List[Any]:
        items: List[Any] = []
        if ctx.telemetry:
            notes = ["Copilot Chat is assumed to send protobuf: the receiver needs "
                     "`pip install 'briefcase-ai[receiver]'`"]
            if ctx.scope == "project":
                notes.append("the Copilot telemetry settings are written to your user settings")
            if ctx.headers:
                notes.append("--headers cannot be set through VS Code settings; export "
                             "OTEL_EXPORTER_OTLP_HEADERS before starting VS Code instead")
            items.append(FileEdit(
                self.id, self.path("user_settings"), "json",
                "Turn on Copilot Chat's OpenTelemetry export and point it at the briefcase receiver",
                ops=[{"op": "set", "path": [key], "value": value} for key, value in self.settings(ctx).items()],
                notes=notes,
            ))
        if ctx.mcp:
            config = mcp_payloads.server_config(which)
            if ctx.scope == "project":
                items.append(FileEdit(
                    self.id, ctx.project_dir / self.data["files"]["project_mcp"], "json",
                    "Register the briefcase MCP server for this workspace",
                    ops=[{"op": "set", "path": ["servers", mcp_payloads.SERVER_NAME],
                          "value": mcp_payloads.stdio_entry(config)}],
                ))
            else:
                items.append(self._mcp_command_or_manual(mcp_payloads.vscode_add_argv(config), None))
        return items


class CopilotCLI(Adapter):
    id = "copilot-cli"

    def _plan(self, ctx: Context) -> List[Any]:
        items: List[Any] = []
        if ctx.telemetry:
            env = {"COPILOT_OTEL_ENABLED": "true", "OTEL_EXPORTER_OTLP_ENDPOINT": ctx.endpoint}
            if ctx.headers:
                env["OTEL_EXPORTER_OTLP_HEADERS"] = _headers_csv(ctx.headers)
            if ctx.capture_content:
                # `copilot help environment`, Copilot CLI 1.0.114: SPAN_ONLY or SPAN_AND_EVENT enables content.
                env["OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT"] = "SPAN_ONLY"
            items.append(Manual(
                self.id, "Add to your shell profile (~/.zshrc, ~/.bashrc, ...)", self.env_exports(env),
                "Copilot CLI reads these from the environment and briefcase does not edit shell "
                "profiles. " + ("It sends protobuf: pip install 'briefcase-ai[receiver]'" if self.needs_protobuf
                                else "It sends OTLP as http/json by default, which the receiver reads as is"),
            ))
        if ctx.mcp:
            config = mcp_payloads.server_config(which)
            items.append(Manual(
                self.id, f"Add to {self.path('user_mcp')}", mcp_payloads.mcp_servers_json(config),
                "the entry shape for this file is not confirmed against Copilot CLI's documentation, "
                "so it is printed rather than written",
            ))
        return items


class GeminiCLI(Adapter):
    id = "gemini-cli"

    def _plan(self, ctx: Context) -> List[Any]:
        if not ctx.telemetry:
            return []
        ops = [{"op": "set", "path": ["telemetry", key], "value": value} for key, value in {
            "enabled": True,
            "target": "local",
            "otlpEndpoint": ctx.endpoint,
            "otlpProtocol": "http",
            "logPrompts": bool(ctx.capture_content),
        }.items()]
        notes = ["experimental: Gemini CLI's own docs say it was superseded in 2026, and its events "
                 "arrive as generic records",
                 "Gemini CLI sends protobuf: the receiver needs `pip install 'briefcase-ai[receiver]'`"]
        purpose = "Point Gemini CLI's OpenTelemetry export at the briefcase receiver"
        if ctx.scope == "project":
            path = ctx.project_dir / self.data["files"]["project_settings"]
            return [FileEdit(self.id, path, "json", purpose, ops=ops, notes=notes)]
        edit = FileEdit(self.id, self.path("user_settings"), "json", purpose, ops=ops, notes=notes)
        if edit.path.exists():
            return [edit]
        return [Manual(self.id, "Add to .gemini/settings.json in a project (or run with --scope project)",
                       snippet(edit),
                       "experimental, and the user-level settings path is not confirmed, so "
                       "it is only written when ~/.gemini/settings.json already exists")]


class Cline(Adapter):
    id = "cline"

    def _plan(self, ctx: Context) -> List[Any]:
        if not ctx.telemetry:
            return []
        env = {
            "CLINE_OTEL_TELEMETRY_ENABLED": "1",
            "CLINE_OTEL_METRICS_EXPORTER": "otlp",
            "CLINE_OTEL_LOGS_EXPORTER": "otlp",
            "CLINE_OTEL_EXPORTER_OTLP_ENDPOINT": ctx.endpoint,
            "CLINE_OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
        }
        return [Manual(self.id, "Set in the environment Cline starts from", self.env_exports(env),
                       "the accepted values for these variables are not confirmed against Cline's "
                       "documentation, so they are printed rather than written. Its events arrive "
                       "as generic records")]


class McpPrintOnly(Adapter):
    """Tools with no telemetry hook and an MCP file format that is not
    confirmed: print the server command and where it probably goes."""

    file_key = "user_mcp"

    def _plan(self, ctx: Context) -> List[Any]:
        if not ctx.mcp:
            return []
        config = mcp_payloads.server_config(which)
        return [Manual(
            self.id, f"Register a stdio MCP server named `{mcp_payloads.SERVER_NAME}` in {self.name}",
            json.dumps(config, indent=2),
            f"{self.name}'s MCP config format (expected at {self.path(self.file_key)}) is not "
            f"confirmed against its documentation, so nothing is written; see {self.docs_url}",
        )]


class Windsurf(McpPrintOnly):
    id = "windsurf"


class OpenCode(McpPrintOnly):
    id = "opencode"
    file_key = "user_config"


_CLASSES = (ClaudeCode, Cowork, Codex, Cursor, VSCodeCopilot, CopilotCLI, GeminiCLI, Cline, Windsurf, OpenCode)
ALIASES = {
    "claude": "claude-code", "claudecode": "claude-code", "claude-desktop": "cowork",
    "vscode": "vscode-copilot", "copilot": "vscode-copilot", "code": "vscode-copilot",
    "gemini": "gemini-cli", "copilot_cli": "copilot-cli",
}


def builtin_ids() -> List[str]:
    return [cls.id for cls in _CLASSES] + list(ALIASES)


def registry(plugins: bool = True) -> Dict[str, Adapter]:
    """Every adapter by id: the built-in ones from ``adapters.json``, then (unless
    ``plugins`` is false) adapters installed packages register under the
    ``briefcase.connect_adapters`` entry point group. A plugin's class carries its own
    ``DATA`` (the keys of an ``adapters.json`` entry); a built-in id always wins."""
    tools = load_data()["tools"]
    out: Dict[str, Adapter] = {cls.id: cls(tools[cls.id]) for cls in _CLASSES}
    if plugins:
        out.update(_plugin_adapters(out))
    return out


def _plugin_adapters(builtin: Dict[str, Adapter]) -> Dict[str, Adapter]:
    from briefcase import plugins as _plugins
    from briefcase._logging import get_logger

    found: Dict[str, Adapter] = {}
    for name, cls in _plugins.get_all("briefcase.connect_adapters", builtin_ids()).items():
        if name in builtin:
            continue
        try:
            adapter = cls(dict(cls.DATA))
            adapter.id = name
        except Exception as exc:  # noqa: BLE001 - one bad plugin must not break `connect`
            get_logger(__name__).warning("connect adapter plugin %r could not be built: %s", name, exc)
            continue
        found[name] = adapter
    return found


def resolve_names(names: List[str]) -> List[str]:
    known = registry()
    out = []
    for raw in names:
        name = ALIASES.get(raw.lower(), raw.lower())
        if name not in known:
            raise KeyError(raw)
        if name not in out:
            out.append(name)
    return out
