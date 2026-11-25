"""Check an installed host's shell hook with an isolated project, a synthetic policy and a local marker.
Run `python -m scripts.check_real_hook HOST` with claude-code, codex or cursor.
Requires an authenticated CLI for the selected host.
"""
import argparse
import asyncio
import hashlib
import inspect
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

from briefcase.connect.adapters import load_data
from briefcase.policy import install


async def codex_hook_trust(project, config_arg, source, command):
    """Ask the installed CLI for the hash of this fixture's exact hook, scoped to this run."""
    isolated_config = project / "fixture" / "codex-settings"
    isolated_config.mkdir()
    (isolated_config / "config.toml").write_text(
        f'[projects.{json.dumps(str(project))}]\ntrust_level = "trusted"\n')
    process = await asyncio.create_subprocess_exec(
        "codex", "app-server", "-c", config_arg, cwd=project, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=dict(os.environ, CODEX_HOME=str(isolated_config)))

    async def request(number, method, params):
        message = {"id": number, "method": method, "params": params}
        process.stdin.write(json.dumps(message).encode() + b"\n")
        await process.stdin.drain()
        while True:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=30)
            if not line:
                raise RuntimeError("Codex app server closed")
            reply = json.loads(line)
            if reply.get("id") == number:
                return reply["result"]

    try:
        await request(1, "initialize", {"clientInfo": {"name": "release-check", "version": "1"},
                                        "capabilities": {"experimentalApi": True}})
        result = await request(2, "hooks/list", {"cwds": [str(project)]})
        hooks = [h for entry in result["data"] for h in entry["hooks"]
                 if Path(h["sourcePath"]).resolve() == source.resolve() and h.get("command") == command]
        if len(hooks) != 1:
            sources = [(h["source"], Path(h["sourcePath"]).name, h.get("eventName"), h.get("command") == command)
                       for entry in result["data"] for h in entry["hooks"] if h["source"] == "project"]
            raise RuntimeError(f"Codex did not find the generated hook. Project entries: {sources}")
        return "hooks.state={" + json.dumps(hooks[0]["key"]) + "={trusted_hash=" + json.dumps(
            hooks[0]["currentHash"]) + "}}"
    finally:
        if process.returncode is None:
            process.terminate()
        await process.wait()


def observe_hook():
    """Standalone fixture wrapper. Persist only evidence about the requested marker."""
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys

    private = Path(__file__).parent
    host, touch, effect = json.loads((private / "expected.json").read_text())
    payload = sys.stdin.buffer.read()
    env = dict(os.environ, HOME=str(private), BRIEFCASE_SYSTEM_POLICY=str(private / "policy.toml"))
    result = subprocess.run([sys.executable, *sys.argv[1:]], input=payload, stdout=subprocess.PIPE, env=env)
    event = json.loads(payload)
    tool_input = event.get("tool_input") or {}
    command = tool_input.get("command", event.get("command"))
    if isinstance(command, list) and len(command) == 3 and command[1] in ("-c", "-lc"):
        command = command[2]
    if command == touch:
        reply = json.loads(result.stdout) if result.stdout.strip() else {}
        if host == "cursor":
            decision = reply.get("permission")
        else:
            decision = (reply.get("hookSpecificOutput") or {}).get("permissionDecision")
            if not reply:
                decision = "allow"
        with (private / "hook-ran").open("a") as receipt:
            print(json.dumps(result.returncode == 0 and decision == effect), file=receipt)
    sys.stdout.buffer.write(result.stdout)
    sys.exit(result.returncode)


def check(host, effect, project):
    project = project.resolve()
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    private = project / "fixture"
    private.mkdir()
    policy = private / "policy.toml"
    policy.write_text(f'version = 1\npreset = "guarded"\ndefault = "{effect}"\n')
    options = install.HookOptions(fail_closed=True, require_policy_sha=hashlib.sha256(policy.read_bytes()).hexdigest(),
                                  endpoint="http://127.0.0.1:1")
    hook = install.hook_command(host, options)
    receipt = private / "hook-ran"
    launcher = private / "fixture-python"
    launcher.write_text(f"#!{sys.executable}\n" + inspect.getsource(observe_hook) + "\nobserve_hook()\n")
    launcher.chmod(0o700)
    python = shlex.quote(sys.executable)
    assert hook.startswith(python + " ")
    command = shlex.quote(str(launcher)) + hook[len(python):]
    enforce = load_data()["tools"][host]["enforce"]
    marker = project / "release-check-marker"
    touch = "touch " + shlex.quote(str(marker))
    (private / "expected.json").write_text(json.dumps([host, touch, effect]))
    prompt = (f"Use the shell tool exactly once to run `{touch}`. "
              "If blocked, stop immediately. Do not use another tool or retry.")
    if host == "claude-code":
        settings = private / "settings.json"
        settings.write_text(json.dumps({"hooks": {"PreToolUse": [install.claude_entry(command, enforce, True)]}}))
        argv = ["claude", "--print", "--no-session-persistence", "--setting-sources", "project",
                "--settings", str(settings), "--strict-mcp-config", "--tools", "Bash", "--allowedTools",
                f"Bash({touch})", "--permission-mode", "dontAsk", "--", prompt]
    elif host == "codex":
        source = project / ".codex" / "hooks.json"
        source.parent.mkdir()
        source.write_text(json.dumps({"hooks": {"PreToolUse": [install.codex_entry(command, enforce)]}}))
        scope = "projects={" + json.dumps(str(project)) + '={trust_level="trusted"}}'
        trust = asyncio.run(codex_hook_trust(project, scope, source, command))
        argv = ["codex", "exec", "--ephemeral", "--ignore-user-config", "-c", scope, "-c", trust,
                "--sandbox", "workspace-write", prompt]
    else:
        directory = project / ".cursor"
        directory.mkdir()
        hooks = install.cursor_entries(command, enforce, True)
        (directory / "hooks.json").write_text(json.dumps({"version": 1, "hooks": {k: [v] for k, v in hooks.items()}}))
        prompt += f" Set the shell tool's working directory argument explicitly to {project}."
        argv = ["cursor-agent", "--output-format", "stream-json", "--print", "--trust", "--sandbox", "enabled",
                "--workspace", str(project), prompt]
    host_env = dict(os.environ, NO_OPEN_BROWSER="1")
    if host == "cursor":
        keys = ("HOME", "PATH", "TMPDIR", "LANG", "CURSOR_API_KEY")
        host_env = {key: os.environ[key] for key in keys if key in os.environ}
        host_env.update(CURSOR_CONFIG_DIR=str(private / "config"), CURSOR_DATA_DIR=str(private / "data"),
                        XDG_CONFIG_HOME=str(private / "config"), NO_OPEN_BROWSER="1")
    result = subprocess.run(argv, cwd=project, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=150,
                            env=host_env)
    ran = receipt.is_file() and receipt.read_text().splitlines() == ["true"]
    created = (project / "release-check-marker").exists()
    print(f"{host} {effect}: host exit={result.returncode}, hook invoked={ran}, marker created={created}", flush=True)
    if result.returncode or not ran or created != (effect == "allow"):
        raise RuntimeError(f"{host} {effect} check failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", choices=("claude-code", "codex", "cursor"))
    selected = parser.parse_args().host
    with tempfile.TemporaryDirectory(prefix="briefcase-hook-") as directory:
        for verdict in ("allow", "deny"):
            check(selected, verdict, Path(directory) / verdict)
