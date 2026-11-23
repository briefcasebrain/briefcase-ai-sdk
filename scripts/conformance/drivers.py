"""One driver per tool. Each knows how to install the tool, what `briefcase-ai connect` should have
done to it, which of the TOOL'S OWN commands confirm that, and whether it can produce telemetry
without credentials.

Install commands are the ones in each vendor's install documentation on the date recorded in
``Driver.install_verified_on``. A tool whose install command could not be verified from
documentation has ``package = None`` and stays at ``docs-only``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .capture import ANTHROPIC_REPLY, ReceiverProcess, Tee, save_payloads
from .core import (CONFIG_ACCEPTED, DOCS_ONLY, REPO_ROOT, TELEMETRY_VERIFIED, Sandbox, ToolResult, briefcase_cli,
                   npm_install, run, wait_for)

Log = Callable[[str], None]


def first_version(text: str) -> Optional[str]:
    import re
    match = re.search(r"\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?", text or "")
    return match.group(0) if match else None


class Driver:
    tool = ""
    binary = ""
    package: Optional[str] = None          # npm package spec, from the vendor's install docs
    install_url = ""                       # the documentation page the command was taken from
    install_verified_on = ""
    connect_args: List[str] = []
    capture_dir_name = ""                  # tests/receiver/fixtures/real/<this>/<version>/

    def __init__(self, existing_binary: bool = False, log: Optional[Log] = None, keep: bool = False,
                 save_dir: Optional[Path] = None) -> None:
        self.existing_binary = existing_binary
        self.log = log or (lambda message: None)
        self.keep = keep
        self.save_dir = save_dir

    # -- steps -----------------------------------------------------------------
    def install_note(self) -> str:
        if self.existing_binary:
            return f"used `{self.binary}` already on PATH (no install)"
        if not self.package:
            return "no install command verified from vendor documentation"
        return (f"npm install -g --prefix <isolated prefix> {self.package} "
                f"(vendor docs {self.install_url}, checked {self.install_verified_on})")

    def install(self, sandbox: Sandbox, result: ToolResult) -> bool:
        if self.existing_binary:
            found = shutil.which(self.binary, path=sandbox.env()["PATH"])
            return result.add("install", found is not None, f"{self.binary} on PATH: {found}")
        done = npm_install(self.package, sandbox, sandbox.env())
        return result.add("install", done.ok, self.install_note() if done.ok else done.out[-400:])

    def connect(self, sandbox: Sandbox, result: ToolResult, extra: Optional[List[str]] = None) -> bool:
        argv = briefcase_cli("connect", self.tool, "--yes", *self.connect_args, *(extra or []))
        done = run(argv, sandbox.env(), cwd=sandbox.project, timeout=180)
        return result.add("briefcase-ai connect --yes", done.ok, done.out[-400:] if not done.ok else "exit 0")

    def verify_config(self, sandbox: Sandbox, result: ToolResult) -> bool:
        raise NotImplementedError

    def telemetry(self, sandbox: Sandbox, result: ToolResult) -> bool:
        """True when real telemetry was captured and mapped. Default: cannot without credentials."""
        result.note = (result.note + " " if result.note else "") + \
            "Telemetry not attempted: the tool needs a logged-in account to emit anything."
        return False

    # -- the whole run -----------------------------------------------------------
    def conform(self) -> ToolResult:
        result = ToolResult(self.tool, install=self.install_note())
        if not self.package and not self.existing_binary:
            result.status = "skipped"
            result.note = self.install_note() + "; staying at docs-only."
            return result
        with Sandbox(keep=self.keep) as sandbox:
            self.log(f"[{self.tool}] installing")
            if not self.install(sandbox, result):
                result.status, result.note = "failed", "install failed: " + result.checks[-1].detail
                return result
            ver = run([self.binary, "--version"], sandbox.env())
            result.version = first_version(ver.out)
            shown = result.version or ver.out.strip()[-120:]
            if not result.add(f"{self.binary} --version", ver.ok and bool(result.version), shown):
                result.status, result.note = "failed", f"`{self.binary} --version` did not work"
                return result
            self.log(f"[{self.tool}] {result.version}: connect + verify")
            if not self.connect(sandbox, result):
                result.status, result.note = "failed", "briefcase-ai connect failed"
                return result
            accepted = self.verify_config(sandbox, result)
            if not accepted:
                result.status, result.note = "failed", "the tool rejected or lost the config connect wrote"
                return result
            result.level = CONFIG_ACCEPTED
            self.log(f"[{self.tool}] config accepted; trying telemetry")
            try:
                if self.telemetry(sandbox, result):
                    result.level = TELEMETRY_VERIFIED
            except Exception as exc:   # a broken telemetry step must not erase config-accepted
                result.add("telemetry step", False, f"{type(exc).__name__}: {exc}")
            return result


class ClaudeCode(Driver):
    tool = "claude-code"
    binary = "claude"
    package = "@anthropic-ai/claude-code@latest"
    install_url = "https://code.claude.com/docs/en/setup"
    install_verified_on = "2026-11-21"
    capture_dir_name = "claude-code"
    plugin_dir = REPO_ROOT / "integrations" / "claude-code" / "briefcase"

    def verify_config(self, sandbox: Sandbox, result: ToolResult) -> bool:
        env = sandbox.env()
        settings = sandbox.home / ".claude" / "settings.json"
        ok = True
        try:
            keys = json.loads(settings.read_text()).get("env", {})
            ok &= result.add("settings.json has the telemetry env keys", "CLAUDE_CODE_ENABLE_TELEMETRY" in keys,
                             ", ".join(sorted(keys)))
        except (OSError, ValueError) as exc:
            ok &= result.add("settings.json has the telemetry env keys", False, str(exc))
        doctor = run(["claude", "doctor"], env, cwd=sandbox.project, timeout=90)
        ok &= result.add("claude doctor reports no invalid settings", "Invalid settings" not in doctor.out,
                         "ok" if "Invalid settings" not in doctor.out else doctor.out[-300:])
        listed = run(["claude", "mcp", "list"], env, cwd=sandbox.project, timeout=120)
        ok &= result.add("claude mcp list shows briefcase", listed.ok and "briefcase" in listed.out,
                         listed.out.strip().splitlines()[-1][:160] if listed.out.strip() else "")
        got = run(["claude", "mcp", "get", "briefcase"], env, cwd=sandbox.project, timeout=120)
        ok &= result.add("claude mcp get briefcase", got.ok and "Scope: User" in got.out, got.out.strip()[:200])
        if self.plugin_dir.is_dir():
            plugin = run(["claude", "plugin", "validate", str(self.plugin_dir)], env, timeout=90)
            ok &= result.add("claude plugin validate (bundled plugin)", plugin.ok and "Validation passed" in plugin.out,
                             plugin.out.strip().splitlines()[-1] if plugin.out.strip() else "")
        return ok

    def telemetry(self, sandbox: Sandbox, result: ToolResult) -> bool:
        env0 = sandbox.env()
        with ReceiverProcess(sandbox, env0) as receiver, Tee(receiver.url, ANTHROPIC_REPLY) as tee:
            # Point the tool at the tee (which forwards OTLP to the real receiver) by re-running connect.
            if not self.connect(sandbox, result, ["--endpoint", tee.url, "--no-mcp"]):
                return False
            env = sandbox.env({
                "ANTHROPIC_BASE_URL": tee.url, "ANTHROPIC_API_KEY": "sk-ant-conformance-dummy",
                "OTEL_LOGS_EXPORT_INTERVAL": "1000", "OTEL_METRIC_EXPORT_INTERVAL": "1000",
            })
            done = run(["claude", "-p", "conformance probe", "--strict-mcp-config"], env, cwd=sandbox.project,
                       timeout=120)
            result.add("claude -p against a local stub model API", done.ok and tee.model_requests > 0,
                       f"model requests seen by stub: {tee.model_requests}; output: {done.out.strip()[-80:]}")
            wait_for(lambda: any(b"user_prompt" in c["body"] for c in tee.captured), 20)
            wait_for(lambda: bool(receiver.records()), 15)
            receiver.stop()
            records = receiver.records()
            logs = [c for c in tee.captured if c["path"] == "/v1/logs"]
            ok = result.add("tool emitted OTLP log events", bool(logs), f"{len(logs)} /v1/logs request(s)")
            kinds = sorted({str(r.get("function_name") or r.get("decision_type") or "?") for r in records})
            ok &= result.add("receiver mapped them to records", bool(records), f"{len(records)} record(s)")
            wanted = [k for k in kinds if "api_request" in k or "user_prompt" in k]
            ok &= result.add("records include the prompt and the model request", len(wanted) >= 2, ", ".join(kinds))
            if self.save_dir and logs:
                target = self.save_dir / self.capture_dir_name / (result.version or "unknown")
                saved = save_payloads(tee.captured, target)
                result.add("raw payloads saved (unscrubbed, temp only)", bool(saved), str(len(saved)))
            self.captured = list(tee.captured)
            return ok


class Codex(Driver):
    tool = "codex"
    binary = "codex"
    package = "@openai/codex"
    install_url = "https://github.com/openai/codex#readme"
    install_verified_on = "2026-11-21"

    def verify_config(self, sandbox: Sandbox, result: ToolResult) -> bool:
        env = sandbox.env()
        ok = True
        listed = run(["codex", "mcp", "list"], env, cwd=sandbox.project)
        bad = "failed to load" in listed.out.lower() or "error:" in listed.out.lower()
        ok &= result.add("codex mcp list loads config.toml and shows briefcase",
                         listed.ok and not bad and "briefcase" in listed.out,
                         " | ".join(listed.stdout.strip().splitlines()[:2])[:200])
        got = run(["codex", "mcp", "get", "briefcase"], env, cwd=sandbox.project)
        ok &= result.add("codex mcp get briefcase", got.ok and "enabled: true" in got.out,
                         " | ".join(got.stdout.strip().splitlines()[:3]))
        doc = run(["codex", "doctor", "--json"], env, cwd=sandbox.project, timeout=90)
        status = parse = None
        try:
            report = json.loads(doc.stdout)
            check = report["checks"]["config.load"]
            status, parse = check["status"], check.get("details", {}).get("config.toml parse")
        except (ValueError, KeyError):
            pass
        ok &= result.add("codex doctor: config.load ok and config.toml parses",
                         status == "ok" and parse == "ok", f"config.load={status} parse={parse}")
        return ok


class GeminiCLI(Driver):
    tool = "gemini-cli"
    binary = "gemini"
    package = "@google/gemini-cli@latest"
    install_url = "https://github.com/google-gemini/gemini-cli#readme"
    install_verified_on = "2026-11-21"
    connect_args = ["--scope", "project", "--no-mcp"]

    def verify_config(self, sandbox: Sandbox, result: ToolResult) -> bool:
        env = sandbox.env()
        path = sandbox.project / ".gemini" / "settings.json"
        ok = True
        try:
            telemetry = json.loads(path.read_text()).get("telemetry", {})
            ok &= result.add(".gemini/settings.json has the telemetry block", telemetry.get("enabled") is True,
                             json.dumps(telemetry)[:160])
        except (OSError, ValueError) as exc:
            ok &= result.add(".gemini/settings.json has the telemetry block", False, str(exc))
        listed = run(["gemini", "mcp", "list"], env, cwd=sandbox.project, timeout=90)
        rejected = "Error in" in listed.out or "Please fix the configuration" in listed.out
        last = listed.out.strip().splitlines()[-1] if listed.out.strip() else ""
        ok &= result.add("gemini loads the project settings without a configuration error",
                         listed.ok and not rejected, last)
        result.note = ("Gemini CLI has no command that echoes its telemetry settings and does not validate their "
                       "values, so acceptance here means: the file loads without a configuration error. "
                       "Telemetry was not captured (the tool needs a logged-in account).")
        return ok

    def telemetry(self, sandbox: Sandbox, result: ToolResult) -> bool:
        return False


class CopilotCLI(Driver):
    tool = "copilot-cli"
    binary = "copilot"
    package = "@github/copilot"
    install_url = "https://github.com/github/copilot-cli#readme"
    install_verified_on = "2026-11-21"
    connect_args = ["--no-mcp"]

    def verify_config(self, sandbox: Sandbox, result: ToolResult) -> bool:
        env = sandbox.env()
        ok = True
        helped = run(["copilot", "help", "environment"], env)
        for var in ("COPILOT_OTEL_ENABLED", "OTEL_EXPORTER_OTLP_ENDPOINT"):
            ok &= result.add(f"`copilot help environment` documents {var}", var in helped.out)
        # The MCP entry that `connect` prints (it writes nothing for this tool): does the tool accept it?
        from briefcase.connect import mcp as mcp_payloads
        target = sandbox.home / ".copilot" / "mcp-config.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"mcpServers": {"briefcase": mcp_payloads.server_config()}}))
        got = run(["copilot", "mcp", "get", "briefcase"], env, cwd=sandbox.project)
        ok &= result.add("`copilot mcp get briefcase` accepts the printed MCP entry",
                         got.ok and "Status: Enabled" in got.out and "Skipping invalid" not in got.out,
                         got.out.strip().splitlines()[0][:120] if got.out.strip() else "")
        return ok

    def conform(self) -> ToolResult:
        result = super().conform()
        if result.status == "ok":
            # connect is print-only for this tool: nothing is written, so no config was "accepted".
            result.level = DOCS_ONLY
            result.note = ("connect is print-only for Copilot CLI (nothing written). The tool's own help documents "
                           "the env vars we print, and it accepts the MCP entry we print; the level stays "
                           "docs-only because no file written by briefcase was loaded. Telemetry needs a login.")
        return result

    def telemetry(self, sandbox: Sandbox, result: ToolResult) -> bool:
        return False


class DocsOnly(Driver):
    """Desktop apps whose install is not scriptable from vendor documentation."""

    reason = ""

    def conform(self) -> ToolResult:
        result = ToolResult(self.tool, status="skipped", install="not installable in CI: " + self.reason)
        result.note = self.reason + " Staying at docs-only."
        return result


class VSCodeCopilot(DocsOnly):
    tool = "vscode-copilot"
    reason = ("VS Code is a desktop app. Its documented `code` CLI manages extensions and MCP entries but has no "
              "command that validates user settings.json, and no settings schema for the Copilot OTel keys was "
              "found in the vendor documentation.")


class Cursor(DocsOnly):
    tool = "cursor"
    reason = ("Cursor is a desktop app with no documented headless install or command that validates "
              "hooks.json, and no published hooks.json schema was found.")


DRIVERS: Dict[str, Any] = {d.tool: d for d in (ClaudeCode, Codex, GeminiCLI, CopilotCLI, VSCodeCopilot, Cursor)}
