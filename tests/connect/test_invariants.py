"""The config writers' invariants, checked over generated states instead of hand-picked cases.

Every combination below builds an existing state for one tool (nothing, an empty file, unrelated content,
partly matching keys, our own earlier write, our write edited or reformatted, one-line JSON holding a fake
secret, wrong types, a credential or a capture switch already set, another profile, a foreign or replaced MCP
registration) and runs one invocation against it (connect with and without flags, `mcp install`, `--force`,
`--scope project`, `policy init`/`--force`/`off`, `--undo`, each also as a dry run). Then:

  I1  no byte of a file we do not own is changed or deleted by a write (insertions only), nothing a user
      replaced or edited is removed by undo (deletions of what we wrote only), and no registry entry changes
  I2  nothing read from an existing file or registry (the fake secret) reaches stdout or stderr
  I3  after a write, the effective settings cannot export content or send a credential this invocation did
      not ask for, and every hook we add names its endpoint
  I4  a profile variable is honoured for every write, and a registry entry that is not exactly ours is never
      added over, replaced or removed
  I5  a dry run exits with the status the real run has; a plan with a failing target writes nothing; "already
      installed" and "Enforcement is off" are only printed when the files say so
  I7  files holding supplied credentials are created 0600

The states and invocations are products of small tables, so a new tool or state is one more row.
"""

import difflib
import itertools
import json
import os
import re
import sys

import pytest

from briefcase.cli.__main__ import main
from briefcase.connect import engine
from briefcase.connect.adapters import registry as adapter_registry

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX paths and modes")

SECRET = "SECRET-1"
SUPPLIED = "SUPPLIED-TOKEN"
EVIL = "https://evil.example.com"

TOOLS = ["claude-code", "codex", "cursor", "vscode-copilot"]
POLICY_TOOLS = ["claude-code", "codex", "cursor"]


# --------------------------------------------------------------------------------------- the world


class FakeCLI:
    """The `claude` registry (keyed by profile and scope), and `codex`/`code` whose `add` replaces silently."""

    def __init__(self, world):
        self.w = world
        self.claude = {}  # (profile, scope) -> argv tail
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[0] == "claude":
            profile = os.environ.get("CLAUDE_CONFIG_DIR")
            if argv[2] == "get":
                entry = self.claude.get((profile, "user"))
                if entry is None:
                    return 1, 'No MCP server found with name: "briefcase"'
                head = "briefcase:\n  Scope: User config\n  Type: stdio\n"
                return 0, head + "  Command: %s\n  Args: %s\n  Environment:" % (entry[0], " ".join(entry[1:]))
            key = (profile, argv[4])
            if argv[2] == "add":
                if key in self.claude:
                    return 1, "MCP server briefcase already exists in %s config" % argv[4]
                self.claude[key] = argv[argv.index("--") + 1:]
                return 0, ""
            if argv[2] == "remove":
                self.claude.pop(key, None)
                return 0, ""
        if argv[0] == "codex" and argv[2] == "add":  # replaces silently, in the profile's config.toml
            path = self.w.codex_dir() / "config.toml"
            text = open(path, newline="").read() if path.exists() else ""  # the tool keeps line endings
            text = re.sub(r"\[mcp_servers\.briefcase\]\n(?:[^\[\n].*\n?)*", "", text)
            tail = argv[argv.index("--") + 1:]
            text += '[mcp_servers.briefcase]\ncommand = %s\nargs = %s\n' % (
                json.dumps(tail[0]), json.dumps(tail[1:]))
            path.parent.mkdir(parents=True, exist_ok=True)
            open(path, "w", newline="").write(text)
            return 0, ""
        if argv[0] == "code" and argv[1] == "--add-mcp":  # replaces silently
            payload = json.loads(argv[2])
            path = self.w.vscode_settings().with_name("mcp.json")
            doc = json.loads(path.read_text()) if path.exists() else {}
            doc.setdefault("servers", {})[payload["name"]] = {k: v for k, v in payload.items() if k != "name"}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(doc))
            return 0, ""
        return 1, "unsupported"


class World:
    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path
        self.home = tmp_path / "home"
        self.project = tmp_path / "project"
        self.bin = tmp_path / "bin"
        for d in (self.home, self.project, self.bin):
            d.mkdir()
        self.mp = monkeypatch
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("USERPROFILE", str(self.home))
        monkeypatch.setenv("PATH", str(self.bin))
        for name in ("BRIEFCASE_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "BRIEFCASE_RECEIVER_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("BRIEFCASE_RECEIVER_ENDPOINT", EVIL)  # the environment a hook would run in
        monkeypatch.chdir(self.project)
        from briefcase.receiver import daemon
        monkeypatch.setattr(daemon, "probe", lambda endpoint, timeout=1.0: None)
        self.cli = FakeCLI(self)
        monkeypatch.setattr(engine, "run_command", self.cli)
        for name in ("claude", "codex", "code"):
            path = self.bin / name
            path.write_text("#!/bin/sh\necho '2.1.330 (fake)'\n")
            path.chmod(0o755)

    def claude_dir(self):
        return __import__("pathlib").Path(os.environ.get("CLAUDE_CONFIG_DIR") or self.home / ".claude")

    def codex_dir(self):
        return __import__("pathlib").Path(os.environ.get("CODEX_HOME") or self.home / ".codex")

    def vscode_settings(self):
        return adapter_registry()["vscode-copilot"].path("user_settings")

    def telemetry_file(self, tool):
        return {"claude-code": self.claude_dir() / "settings.json", "codex": self.codex_dir() / "config.toml",
                "cursor": self.home / ".cursor" / "hooks.json", "vscode-copilot": self.vscode_settings()}[tool]

    def snapshot(self):
        files = {}
        for base in (self.home, self.project):
            for path in sorted(base.rglob("*")):
                rel = str(path.relative_to(self.root))
                if path.is_file() and "/.briefcase/connect/" not in "/" + rel and ".briefcase-bak-" not in rel \
                        and "/.briefcase/policy/" not in "/" + rel:
                    files[rel] = (path.read_bytes(), path.stat().st_mode & 0o777)
        return {"files": files, "claude": dict(self.cli.claude)}

    def run(self, argv):
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------------------- states

JSON_SEEDS = {
    "claude-code": {
        "unrelated": json.dumps({"model": "opus", "env": {"FOO": "bar"}}, indent=2) + "\n",
        "compact_secret": '{"env":{"API_KEY":"%s"},"mcpServers":{"o":{"args":["--token=%s"]}}}' % (SECRET, SECRET),
        "partial": json.dumps({"env": {"OTEL_METRICS_EXPORTER": "console"}}, indent=2) + "\n",
        "capture_on": json.dumps({"env": {"OTEL_LOG_USER_PROMPTS": "1"}}, indent=2) + "\n",
        "headers_on": json.dumps({"env": {"OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer " + SECRET}}) + "\n",
        "wrong_types": '{"env": "nope"}\n',
        "raw_bodies": json.dumps({"env": {"OTEL_LOG_RAW_API_BODIES": "1"}}) + "\n",
        "helper": json.dumps({"otelHeadersHelper": "/bin/get-token"}) + "\n",
        "per_signal": json.dumps({"env": {"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "https://elsewhere.example.com"}}) + "\n",
        "duplicate_keys": '{"env": {"A": "1"}, "env": {"B": "2"}}\n',
        "crlf": '{\r\n  "model": "opus"\r\n}\r\n',
    },
    "vscode-copilot": {
        "unrelated": '{\n    "editor.fontSize": 14\n}\n',
        "compact_secret": '{"x":"%s","editor.fontSize":14}' % SECRET,
        "partial": '{"github.copilot.chat.otel.enabled": false}\n',
        "capture_on": '{"github.copilot.chat.otel.captureContent": true}\n',
        "wrong_types": "[1, 2]\n",
        "otel_other": '{"github.copilot.chat.otel.logLevel": "debug"}\n',
        "duplicate_keys": '{"a": 1, "a": 2}\n',
        "crlf": '{\r\n    "editor.fontSize": 14\r\n}\r\n',
    },
    "cursor": {
        "unrelated": json.dumps({"version": 1, "hooks": {"stop": [{"command": "./mine.sh"}]}}, indent=2) + "\n",
        "compact_secret": '{"hooks":{"stop":[{"command":"x --token=%s"}]}}' % SECRET,
        "partial": '{"version": 2}\n',
        "wrong_types": '{"hooks": []}\n',
        "duplicate_keys": '{"hooks": {}, "hooks": {"stop": []}}\n',
        "crlf": '{\r\n  "version": 1\r\n}\r\n',
    },
    "codex": {
        "unrelated": 'model = "gpt-5"\n\n[tui]\nnotifications = true\n',
        "compact_secret": '[mcp_servers.other]\ncommand = "x"\nargs = ["--token=%s"]' % SECRET,
        "partial": '[otel]\nenvironment = "prod"\n',
        "capture_on": '[otel]\nlog_user_prompt = true\n',
        "headers_on": '[otel]\nexporter = { otlp-http = { endpoint = "https://x", headers = { "Authorization" = '
                      '"Bearer %s" } } }\n' % SECRET,
        "wrong_types": "model = \n",
        "toml_multiline": 'developer_instructions = """\nfirst\n[otel]\nsecond\n"""\nmodel = "gpt-5"\n',
        "crlf": 'model = "gpt-5"\r\n\r\n[tui]\r\nnotifications = true\r\n',
    },
}
OWNED_STATES = ["ours", "ours_edited", "ours_reformatted", "ours_in_unrelated"]
REGISTRY_STATES = ["registry_foreign", "registry_ours_replaced"]
PROFILE_STATES = ["other_profile", "relative_profile"]
POLICY_STATES = ["policy_installed", "policy_installed_copy", "policy_installed_edited"]
STATES = ["absent", "empty"] + OWNED_STATES + REGISTRY_STATES + PROFILE_STATES


def states_for(tool):
    out = ["absent", "empty"] + list(JSON_SEEDS[tool])
    out += OWNED_STATES
    out += [s for s in REGISTRY_STATES if tool != "cursor"]
    out += [s for s in PROFILE_STATES if tool in ("claude-code", "codex")]
    out += [s for s in POLICY_STATES if tool in POLICY_TOOLS]
    return out


def edit_owned(world, tool, how, endpoint="127.0.0.1:4318"):
    """Edit a file we wrote: ``edited`` changes one value we wrote, ``reformatted`` only spacing/order."""
    path = world.telemetry_file(tool)
    text = path.read_text()
    if how == "edited":
        if tool == "codex":
            text = text.replace(endpoint, "mine.example.com")
        elif tool == "cursor":
            text = text.replace('"timeout": 5', '"timeout": 6')
        else:
            text = text.replace(endpoint, "mine.example.com")
    else:
        if tool == "codex":
            text = text.replace("\n[", "\n\n[").rstrip("\n")  # spacing and no trailing newline
        else:
            doc = json.loads(text)
            text = json.dumps(doc, separators=(",", ":"))  # one line, no trailing newline
    path.write_text(text)


def seed(world, tool, state):
    """Build ``state`` for ``tool``; returns the argv that connected it first (so tests can reuse it)."""
    path = world.telemetry_file(tool)
    if state == "absent":
        return
    if state in ("other_profile", "relative_profile"):
        var = "CLAUDE_CONFIG_DIR" if tool == "claude-code" else "CODEX_HOME"
        decoy = world.home / (".claude" if tool == "claude-code" else ".codex")
        decoy.mkdir(parents=True, exist_ok=True)
        name = "settings.json" if tool == "claude-code" else "config.toml"
        (decoy / name).write_text(JSON_SEEDS[tool]["unrelated"])
        if state == "other_profile":
            (world.root / "profile").mkdir()
            world.mp.setenv(var, str(world.root / "profile"))
        else:
            world.mp.setenv(var, "relative/profile")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if state == "empty":
        path.write_text("")
    elif state in JSON_SEEDS[tool]:
        path.write_text(JSON_SEEDS[tool][state])
        if tool == "claude-code" and state == "compact_secret":
            (world.project / ".mcp.json").write_text(JSON_SEEDS[tool][state])
    elif state in OWNED_STATES:
        if state == "ours_in_unrelated":
            path.write_text(JSON_SEEDS[tool]["unrelated"])
        code, _, _ = world.run(["connect", tool, "--yes", "--no-mcp"])
        assert code == 0
        if state == "ours_edited":
            edit_owned(world, tool, "edited")
        elif state == "ours_reformatted":
            edit_owned(world, tool, "reformatted")
    elif state in POLICY_STATES:
        assert world.run(["policy", "init", tool, "--yes"])[0] == 0
        hooks_file = world.telemetry_file(tool) if tool != "codex" else world.codex_dir() / "hooks.json"
        doc = json.loads(hooks_file.read_text())
        needle = f" {tool} --enforce"

        def walk(node):
            if isinstance(node, list):
                for item in list(node):
                    if isinstance(item, dict) and needle in json.dumps(item):
                        clone = json.loads(json.dumps(item).replace('"timeout": ', '"timeout": 9'))
                        if state == "policy_installed_edited":
                            node[node.index(item)] = clone
                        elif state == "policy_installed_copy":
                            node.append(clone)
                    else:
                        walk(item)
            elif isinstance(node, dict):
                for value in node.values():
                    walk(value)
        if state != "policy_installed":
            walk(doc)
            hooks_file.write_text(json.dumps(doc, indent=2) + "\n")
    elif state == "registry_foreign":
        foreign_registry(world, tool)
    elif state == "registry_ours_replaced":
        code, _, _ = world.run(["mcp", "install", tool, "--yes"])
        assert code == 0
        foreign_registry(world, tool, replace=True)


def foreign_registry(world, tool, replace=False):
    if tool == "claude-code":
        world.cli.claude[(os.environ.get("CLAUDE_CONFIG_DIR"), "user")] = ["node", "server.js", "--token=" + SECRET]
    elif tool == "codex":
        path = world.codex_dir() / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text() if replace else ""
        text = re.sub(r"\[mcp_servers\.briefcase\]\n(?:[^\[\n].*\n?)*", "", text)
        path.write_text(text + '[mcp_servers.briefcase]\ncommand = "node"\nargs = ["--token=%s"]\n' % SECRET)
    elif tool == "vscode-copilot":
        path = world.vscode_settings().with_name("mcp.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"servers": {"briefcase": {"command": "node", "args": ["--token=" + SECRET]}}}))


# --------------------------------------------------------------------------------------- invocations

FLAGS = ["--endpoint", "https://new.example.com", "--headers", "Authorization=Bearer " + SUPPLIED,
         "--capture-content"]
WRITE, REMOVE = "write", "remove"


def invocations(tool):
    out = {
        "connect": (["connect", tool, "--yes"], WRITE),
        "connect_no_mcp": (["connect", tool, "--yes", "--no-mcp"], WRITE),
        "connect_flags": (["connect", tool, "--yes", *FLAGS], WRITE),
        "connect_endpoint": (["connect", tool, "--yes", "--endpoint", "https://new.example.com"], WRITE),
        "connect_project": (["connect", tool, "--yes", "--scope", "project"], WRITE),
        "connect_force": (["connect", tool, "--yes", "--force"], WRITE),
        "mcp_install": (["mcp", "install", tool, "--yes"], WRITE),
        "undo": (["connect", "--undo", tool], REMOVE),
    }
    if tool in POLICY_TOOLS:
        out.update({
            "policy_init": (["policy", "init", tool, "--yes"], WRITE),
            "policy_init_force": (["policy", "init", tool, "--yes", "--force"], WRITE),
            "policy_off": (["policy", "off", tool, "--yes"], REMOVE),
        })
    return out


COMBOS = [(tool, state, name) for tool in TOOLS for state in states_for(tool) for name in invocations(tool)]


# --------------------------------------------------------------------------------------- invariants


def opcodes(old: bytes, new: bytes):
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    return {tag for tag, *_ in matcher.get_opcodes()} - {"equal"}


def content_secrets_present(world_before):
    blobs = [data for data, _ in world_before["files"].values()]
    registered = " ".join(" ".join(v) for v in world_before["claude"].values())
    return any(SECRET.encode() in b for b in blobs) or SECRET in registered


def marker_present(world, tool):
    needle = f" {tool} --enforce".encode()
    for path in itertools.chain(world.home.rglob("*.json"), world.project.rglob("*.json")):
        if path.is_file() and needle in path.read_bytes():
            return True
    return False


def namespace_keys(tool, text):
    """Keys in the tool's telemetry namespace, judged here and not from the adapter's own lists."""
    try:
        if tool == "codex":
            from briefcase.connect import edits
            return ["otel"] if "otel" in edits._toml.loads(text) else []
        doc = json.loads(text)
    except Exception:
        return []
    if tool == "claude-code":
        env = doc.get("env") if isinstance(doc.get("env"), dict) else {}
        return [k for k in env if k.upper().startswith("OTEL_")] + [k for k in doc if k.lower().startswith("otel")]
    if tool == "vscode-copilot":
        return [k for k in doc if k.lower().startswith("github.copilot.chat.otel")]
    if tool == "gemini-cli":
        return list(doc.get("telemetry", {}))
    return []


def effective_exports(world, tool):
    """(captures content, sends a credential) according to the tool's own settings file as it is now."""
    path = world.telemetry_file(tool)
    if not path.exists():
        return False, False
    try:
        if tool == "codex":
            from briefcase.connect import edits
            doc = edits._toml.loads(path.read_text())
            otel = doc.get("otel", {})
            return bool(otel.get("log_user_prompt")), "headers" in json.dumps(otel).replace(" ", "").lower() and \
                '"headers":{}' not in json.dumps(otel).replace(" ", "").lower()
        doc = json.loads(path.read_text())
    except Exception:
        return False, False
    if tool == "claude-code":
        env = doc.get("env", {}) if isinstance(doc.get("env"), dict) else {}
        cap = any(str(env.get(k, "")).lower() in ("1", "true") for k in
                  ("OTEL_LOG_USER_PROMPTS", "OTEL_LOG_TOOL_DETAILS", "OTEL_LOG_TOOL_CONTENT"))
        return cap, any(env.get(k) for k in ("OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
                                             "OTEL_EXPORTER_OTLP_METRICS_HEADERS"))
    if tool == "vscode-copilot":
        return doc.get("github.copilot.chat.otel.captureContent") is True, False
    return False, False


@pytest.mark.parametrize("tool, state, name", COMBOS, ids=[f"{t}-{s}-{n}" for t, s, n in COMBOS])
def test_invariants(tmp_path, monkeypatch, tool, state, name):
    world = World(tmp_path, monkeypatch)
    seed(world, tool, state)
    argv, kind = invocations(tool)[name]
    before = world.snapshot()
    calls_before = len(world.cli.calls)

    dry_code, dry_out, dry_err = world.run(argv + ["--dry-run"])
    assert world.snapshot() == before, "a dry run changed something"
    code, out, err = world.run(argv)
    after = world.snapshot()
    text = out + err
    asked_capture, asked_headers = "--capture-content" in argv, "--headers" in argv

    # I5: a dry run exits with the status the real run has
    assert dry_code == code, f"dry run exited {dry_code}, the real run {code}\n{dry_out}{dry_err}\n{text}"

    # I2: nothing read from an existing file or registry is shown
    if content_secrets_present(before):
        assert SECRET not in text, "an existing secret was printed"

    # I1: files
    for rel, (old, _) in before["files"].items():
        if rel.endswith("policy.toml"):
            assert after["files"].get(rel, (None,))[0] == old, rel
            continue
        new = after["files"].get(rel, (None, 0))[0]
        if kind == WRITE:
            assert new is not None, f"{rel} was deleted by a write"
            assert opcodes(old, new) <= {"insert"}, f"{rel}: a write changed bytes it does not own"
        else:
            if new is None:
                assert (state.startswith("ours") and state != "ours_in_unrelated") or state in POLICY_STATES, \
                    f"{rel} deleted by undo"
            else:
                assert opcodes(old, new) <= {"delete"}, f"{rel}: undo changed bytes it did not write"
    # I1: registry
    for key, value in before["claude"].items():
        assert after["claude"].get(key) == value, "a registration changed"
    new_calls = world.cli.calls[calls_before:]
    if state in REGISTRY_STATES:
        writes = (["claude", "mcp", "add"], ["claude", "mcp", "remove"], ["codex", "mcp", "add"])
        assert not [c for c in new_calls if c[:3] in writes or c[:2] == ["code", "--add-mcp"]], \
            "added over, or removed, an entry that is not ours"
        for rel, (old, _) in before["files"].items():
            if tool == "codex" and rel.endswith("config.toml"):  # the registry shares the telemetry file
                from briefcase.connect import edits
                was = edits._toml.loads(old.decode()).get("mcp_servers")
                assert edits._toml.loads(after["files"][rel][0].decode()).get("mcp_servers") == was, "registry changed"
            else:
                assert after["files"][rel][0] == old, f"{rel}: a registry file changed"
    # `remove` is never run by us at all
    assert not [c for c in new_calls if c[:3] == ["claude", "mcp", "remove"]]

    # I4: a profile variable is honoured
    if state == "other_profile" and kind == WRITE and code == 0 and name not in ("connect_project",):
        decoy = world.home / (".claude" if tool == "claude-code" else ".codex")
        for rel, (old, _) in before["files"].items():
            if str(decoy) in str(world.root / rel):
                assert after["files"][rel][0] == old, "the default profile was written"
    if state == "relative_profile" and kind == WRITE and name != "mcp_install":
        assert code == 1 and after["files"] == before["files"], "an unresolvable profile must refuse"

    # I5: a failing plan writes nothing; claims about the disk are true
    if "Nothing was written" in out:
        assert after["files"] == before["files"] and code == 1
    if "already installed" in out:
        assert marker_present(world, tool)
    if "Enforcement is off" in out:
        assert not marker_present(world, tool)
    if name.startswith("policy_init") and code == 0 and "--dry-run" not in argv:
        assert marker_present(world, tool), "policy init succeeded but no enforcement hook is on disk"
    if name == "policy_off" and code == 0 and "No policy hooks are installed" not in out:
        assert not marker_present(world, tool)

    # I3: nothing is exported that was not asked for; every hook names its endpoint
    wrote_telemetry = f"wrote  {world.telemetry_file(tool).resolve()}" in out or \
        f"wrote  {world.telemetry_file(tool)}" in out
    if kind == WRITE and wrote_telemetry and tool != "cursor" and not name.startswith("policy"):
        cap, creds = effective_exports(world, tool)
        was_cap, was_creds = effective_exports_from(before, world, tool)
        if cap and not was_cap:
            assert asked_capture, "content capture was switched on without being asked"
        if cap and was_cap and not asked_capture:
            pytest.fail("existing content capture now exports to the new endpoint")
        if creds and was_creds and not asked_headers:
            pytest.fail("an existing credential now goes to the new endpoint")
    if kind == WRITE and wrote_telemetry and tool != "cursor" and not name.startswith("policy"):
        rel = str(world.telemetry_file(tool).relative_to(world.root))
        existing = namespace_keys(tool, before["files"].get(rel, (b"",))[0].decode("utf-8", "replace"))
        assert not existing, f"wrote telemetry next to existing telemetry settings {existing}"
    if state == "duplicate_keys" and kind == WRITE:
        rel = str(world.telemetry_file(tool).relative_to(world.root))
        assert after["files"][rel] == before["files"][rel], "an ambiguous document was edited"
    if tool == "cursor" and kind == WRITE:
        path = world.telemetry_file("cursor")
        if path.exists():
            try:
                hooks = json.loads(path.read_text()).get("hooks", {})
            except Exception:
                hooks = {}
            ours = [h["command"] for entries in (hooks.values() if isinstance(hooks, dict) else [])
                    if isinstance(entries, list) for h in entries
                    if isinstance(h, dict) and "hook cursor" in h.get("command", "")]
            assert all("--endpoint" in c for c in ours), ours

    # I7: every file that holds a supplied credential is private, new or not; and it is never printed
    for rel, (data, mode) in after["files"].items():
        if SUPPLIED.encode() in data:
            assert mode == 0o600, f"{rel} holds a supplied credential with mode {oct(mode)}"
    assert SUPPLIED not in text + dry_out + dry_err, "a supplied credential was printed"


def effective_exports_from(before, world, tool):
    """The same question asked of the file as it was before the run."""
    rel = str(world.telemetry_file(tool).relative_to(world.root))
    data = before["files"].get(rel, (b"", 0))[0].decode("utf-8", "replace")
    probe = world.root / "probe" / "settings"
    probe.parent.mkdir(exist_ok=True)
    original = world.telemetry_file
    try:
        probe.write_text(data)
        world.telemetry_file = lambda t: probe  # type: ignore[method-assign]
        return effective_exports(world, tool)
    finally:
        world.telemetry_file = original  # type: ignore[method-assign]


# --------------------------------------------------------------------------------------- follow the instruction


@pytest.mark.parametrize("tool", ["claude-code", "codex", "cursor", "vscode-copilot"])
def test_the_printed_recovery_instruction_ends_connected_when_followed_literally(tmp_path, monkeypatch, tool):
    world = World(tmp_path, monkeypatch)
    code, _, _ = world.run(["connect", tool, "--yes", "--no-mcp"])
    assert code == 0
    # Asking for other settings: the instruction is "--undo, then connect again with the new settings".
    code, out, _ = world.run(["connect", tool, "--yes", "--no-mcp", "--endpoint", "https://new.example.com"])
    assert code == 1 and f"connect --undo {tool}" in out
    assert world.run(["connect", "--undo", tool])[0] == 0
    code, out, _ = world.run(["connect", tool, "--yes", "--no-mcp", "--endpoint", "https://new.example.com"])
    assert code == 0 and "new.example.com" in world.telemetry_file(tool).read_text()
    assert "already connected" in world.run(["connect", tool, "--yes", "--no-mcp"])[1]
    # An edited target: "remove the keys yourself, undo, connect again".
    edit_owned(world, tool, "edited", endpoint="new.example.com")
    code, out, _ = world.run(["connect", tool, "--yes", "--no-mcp"])
    assert code == 0 and "modified since connect" in out and "remove the briefcase keys" in out
    path = world.telemetry_file(tool)
    path.unlink()  # the manual step the instruction names
    assert world.run(["connect", "--undo", tool])[0] == 0
    assert world.run(["connect", tool, "--yes", "--no-mcp"])[0] == 0
    assert "already connected" in world.run(["connect", tool, "--yes", "--no-mcp"])[1]


@pytest.mark.parametrize("tool", ["claude-code", "codex", "cursor", "vscode-copilot"])
def test_a_tool_that_reformats_or_reorders_our_content_does_not_make_it_modified(tmp_path, monkeypatch, tool):
    world = World(tmp_path, monkeypatch)
    assert world.run(["connect", tool, "--yes", "--no-mcp"])[0] == 0
    edit_owned(world, tool, "reformatted")
    code, out, _ = world.run(["connect", tool, "--yes", "--no-mcp"])
    assert code == 0 and "already connected" in out and "modified" not in out


def test_a_table_the_tool_inserts_inside_our_markers_is_not_ours_and_survives_undo(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    assert world.run(["connect", "codex", "--yes", "--no-mcp"])[0] == 0
    path = world.telemetry_file("codex")
    inserted = '[projects."/x"]\ntrust_level = "trusted"\n# <<< briefcase <<<'
    text = path.read_text().replace("# <<< briefcase <<<", inserted)
    path.write_text(text)
    code, out, _ = world.run(["connect", "codex", "--yes", "--no-mcp"])
    assert code == 0 and "already connected" in out
    assert world.run(["connect", "--undo", "codex"])[0] == 0
    left = path.read_text()
    assert '[projects."/x"]' in left and "[otel]" not in left


ROUND_TRIP = [(t, st) for t in TOOLS for st in ("unrelated", "compact_secret", "crlf", "toml_multiline")
              if st in JSON_SEEDS[t]]


@pytest.mark.parametrize("tool, state", ROUND_TRIP)
def test_undo_returns_the_file_to_its_original_bytes_whatever_it_held(tmp_path, monkeypatch, tool, state):
    world = World(tmp_path, monkeypatch)
    seed(world, tool, state)
    path = world.telemetry_file(tool)
    original = path.read_bytes()
    assert world.run(["connect", tool, "--yes", "--no-mcp"])[0] == 0
    assert path.read_bytes() != original
    assert world.run(["connect", "--undo", tool])[0] == 0
    assert path.read_bytes() == original, "undo must give back exactly the user's bytes"
    if state == "toml_multiline":
        from briefcase.connect import edits
        assert "[otel]" in path.read_text() and edits._toml.loads(path.read_text())["model"] == "gpt-5"


@pytest.mark.parametrize("tool", ["claude-code", "codex", "cursor", "vscode-copilot"])
def test_undo_returns_the_file_to_its_original_bytes(tmp_path, monkeypatch, tool):
    world = World(tmp_path, monkeypatch)
    seed(world, tool, "unrelated")
    path = world.telemetry_file(tool)
    original = path.read_bytes()
    assert world.run(["connect", tool, "--yes", "--no-mcp"])[0] == 0
    assert path.read_bytes() != original
    assert world.run(["connect", "--undo", tool])[0] == 0
    assert path.read_bytes() == original


def test_one_line_json_is_extended_without_reformatting_a_byte_of_it(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    seed(world, "claude-code", "compact_secret")
    path = world.telemetry_file("claude-code")
    original = path.read_text()
    assert world.run(["connect", "claude-code", "--yes", "--no-mcp"])[0] == 0
    assert opcodes(original.encode(), path.read_bytes()) == {"insert"}
    assert json.loads(path.read_text())["env"]["API_KEY"] == SECRET


def test_a_true_changed_to_one_is_not_removed_by_undo(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    assert world.run(["connect", "claude-code", "--yes", "--no-mcp", "--capture-content"])[0] == 0
    path = world.telemetry_file("claude-code")
    doc = json.loads(path.read_text())
    doc["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] = "1"
    path.write_text(json.dumps(doc))
    from briefcase.connect import edits
    assert not edits.typed_eq(True, 1) and not edits.typed_eq(1, 1.0) and edits.typed_eq({"a": [True]}, {"a": [True]})


def test_a_file_created_after_the_preview_is_not_overwritten(tmp_path, monkeypatch):
    from briefcase.connect import edits
    from briefcase.connect.adapters import Context

    world = World(tmp_path, monkeypatch)
    ctx = Context(mcp=False)
    plan = engine.build_plan(["claude-code"], ctx, runner=world.cli)
    target = world.telemetry_file("claude-code")
    target.parent.mkdir(parents=True)
    target.write_text('{"mine": "SECRET-1"}')
    out = []
    assert engine.apply_plan(plan, ctx, out.append, runner=world.cli) == 1
    assert target.read_text() == '{"mine": "SECRET-1"}' and "appeared after the preview" in out[0]
    assert not list(target.parent.glob("*.briefcase-bak-*")) and edits.load_journal()["targets"] == {}


# --------------------------------------------------------------------------------------- managed packs

PACK_STATES = ["absent", "empty_dir", "unrelated_files", "earlier_pack", "earlier_pack_edited", "compact_secret"]


@pytest.mark.parametrize("state, flags", [(s, f) for s in PACK_STATES for f in
                                          ([], ["--force"], ["--headers", "Authorization=Bearer " + SUPPLIED])])
def test_managed_output_never_touches_what_it_did_not_write(tmp_path, monkeypatch, state, flags):
    world = World(tmp_path, monkeypatch)
    out_dir = tmp_path / "pack"
    base = ["connect", "--managed", "--out", str(out_dir), "--endpoint", "https://otel.example.com"]
    if state != "absent":
        out_dir.mkdir()
    if state == "unrelated_files":
        (out_dir / "README.md").write_text("mine " + SECRET)
        (out_dir / "manifest.json").write_text('{"mine": "%s"}' % SECRET)
    elif state == "compact_secret":
        (out_dir / "notes.txt").write_text(SECRET)
    elif state.startswith("earlier_pack"):
        assert world.run(base)[0] == 0
        if state.endswith("edited"):
            (out_dir / "README.md").write_text("edited by hand")
    before = {str(p.relative_to(out_dir)): p.read_bytes() for p in out_dir.rglob("*") if p.is_file()} \
        if out_dir.exists() else {}
    dry_code, _, _ = world.run(base + flags + ["--dry-run"])
    code, out, err = world.run(base + flags)
    assert dry_code == code
    assert SECRET not in out + err
    after = {str(p.relative_to(out_dir)): p.read_bytes() for p in out_dir.rglob("*") if p.is_file()} \
        if out_dir.exists() else {}
    foreign = state in ("unrelated_files", "compact_secret", "earlier_pack_edited")
    for rel, data in before.items():
        if foreign and rel in ("README.md", "manifest.json", "notes.txt"):
            if state == "earlier_pack_edited" and rel != "README.md":
                continue
            assert after[rel] == data, f"{rel} was overwritten"
    if foreign:
        assert code == 1
    if "--headers" in flags and code == 0:
        modes = {p.stat().st_mode & 0o777 for p in out_dir.rglob("*") if p.is_file() and p.name not in before}
        assert modes <= {0o600}, modes


def combos_count():
    return len(COMBOS)


def test_the_generated_product_is_large_enough_to_mean_something():
    assert combos_count() >= 300, combos_count()
