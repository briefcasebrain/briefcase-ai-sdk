"""`briefcase-ai connect --managed --out DIR`: golden files for the rollout pack, plus the rules that
nothing is ever written to a system path and unverified outputs are labelled templates.

Regenerate the golden files after an intended change with:
    BRIEFCASE_UPDATE_GOLDEN=1 pytest tests/policy/test_managed.py
"""

import json
import os
import plistlib
from argparse import Namespace
from pathlib import Path

import pytest

from briefcase.policy import managed, model

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX paths")

GOLDEN = Path(__file__).parent / "golden" / "managed"
ENDPOINT = "https://otel.example.com"
POLICY = 'version = 1\npreset = "guarded"\n\n[[rule]]\nid = "no-prod"\ntool = "shell"\neffect = "deny"\n' \
         'command = "kubectl --context=prod*"\nreason = "Production goes through the runbook."\n'


def args(out, **overrides):
    values = dict(out=None if out is None else str(out), endpoint=ENDPOINT, policy=None, managed_tools=None,
                  tools=[], headers=[], capture_content=False, force=False, connect_dry_run=False, dry_run=False)
    values.update(overrides)
    return Namespace(**values)


def generate(tmp_path, **overrides):
    policy = tmp_path / "policy.toml"
    policy.write_text(POLICY)
    out = tmp_path / "pack"
    lines = []
    code = managed.cmd_managed(args(out, policy=str(policy), **overrides), out=lines.append)
    return code, out, "\n".join(lines)


def tree(root):
    return {p.relative_to(root).as_posix(): p.read_text() for p in sorted(root.rglob("*")) if p.is_file()}


def test_pack_matches_the_golden_files(home, tmp_path):
    code, out, printed = generate(tmp_path)
    assert code == 0, printed
    got = tree(out)
    if os.environ.get("BRIEFCASE_UPDATE_GOLDEN") == "1":
        import shutil
        shutil.rmtree(GOLDEN, ignore_errors=True)
        for name, text in got.items():
            (GOLDEN / name).parent.mkdir(parents=True, exist_ok=True)
            (GOLDEN / name).write_text(text)
    assert got == tree(GOLDEN), "golden files are stale; see the module docstring to regenerate"


def test_every_unconfirmed_output_is_a_labelled_template(home, tmp_path):
    _, out, _ = generate(tmp_path)
    manifest = json.loads((out / "manifest.json").read_text())
    for entry in manifest["files"]:
        text = (out / entry["file"]).read_text()
        if entry["kind"] == "template":
            assert "TEMPLATE" in text.splitlines()[0] or "TEMPLATE" in text[:400], entry["file"]
            assert entry["file"].endswith(".template") or entry["file"].endswith("CHECKLIST.md")
        else:
            assert entry["source"].startswith(("https://", "briefcase")), entry
    readme = (out / "README.md").read_text()
    for entry in manifest["files"]:
        assert f"`{entry['file']}`" in readme
    assert "briefcase-ai doctor --connect" in readme


def test_generated_files_parse_in_their_formats(home, tmp_path):
    _, out, _ = generate(tmp_path)
    settings = json.loads((out / "claude-code/managed-settings.d/10-briefcase.json").read_text())
    assert settings["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == ENDPOINT
    handler = settings["hooks"]["PreToolUse"][0]["hooks"][0]
    assert handler["command"] == f"briefcase-ai hook claude-code --enforce --endpoint {ENDPOINT}"
    # Only keys the managed-settings documentation lists; no lock settings are added silently.
    assert set(settings) == {"env", "hooks"}
    profile = plistlib.loads((out / "claude-code/macos/briefcase-claude-code.mobileconfig").read_bytes())
    payload = profile["PayloadContent"][0]
    assert payload["PayloadType"] == "com.apple.ManagedClient.preferences"
    forced = payload["PayloadContent"]["com.anthropic.claudecode"]["Forced"][0]["mcx_preference_settings"]
    assert forced == settings
    cursor = json.loads((out / "cursor/hooks.json").read_text())
    assert cursor["version"] == 1 and "--enforce" in cursor["hooks"]["beforeShellExecution"][0]["command"]
    codex = json.loads((out / "codex/hooks.json").read_text())
    assert codex["hooks"]["PreToolUse"][0]["matcher"] == "^(Bash|apply_patch)$|^mcp__"
    layer = model.parse_layer((out / "briefcase/policy.toml").read_text(), "system")
    assert [rule.id for rule in layer.rules] == ["no-prod"]
    try:
        import tomllib
    except ImportError:  # pragma: no cover
        tomllib = None
    if tomllib is not None:
        body = (out / "codex/requirements-hooks.toml.template").read_text()
        assert tomllib.loads(body)["hooks"]["PreToolUse"][0]["hooks"][0]["type"] == "command"


def test_fail_closed_policy_adds_the_documented_failure_fields(home, tmp_path):
    policy = tmp_path / "closed.toml"
    policy.write_text(POLICY.replace('preset = "guarded"', 'preset = "guarded"\nfail_closed = true'))
    out = tmp_path / "closed"
    assert managed.cmd_managed(args(out, policy=str(policy)), out=lambda line: None) == 0
    handler = json.loads((out / "claude-code/managed-settings.d/10-briefcase.json").read_text())[
        "hooks"]["PreToolUse"][0]["hooks"][0]
    assert handler["onFailure"] == "block" and handler["command"].endswith("--fail-closed --endpoint " + ENDPOINT)
    cursor = json.loads((out / "cursor/hooks.json").read_text())
    assert cursor["hooks"]["beforeReadFile"][-1]["failClosed"] is True
    assert "BLOCKED" in (out / "README.md").read_text()


def test_without_a_policy_only_telemetry_is_generated(home, tmp_path):
    out = tmp_path / "telemetry"
    lines = []
    assert managed.cmd_managed(args(out), out=lines.append) == 0
    settings = json.loads((out / "claude-code/managed-settings.d/10-briefcase.json").read_text())
    assert "hooks" not in settings
    assert not (out / "codex/hooks.json").exists() and not (out / "briefcase/policy.toml").exists()
    assert all("--enforce" not in entry[0]["command"]
               for entry in json.loads((out / "cursor/hooks.json").read_text())["hooks"].values())
    assert any("enforces nothing" in line for line in lines)


def test_tools_subset(home, tmp_path):
    code, out, _ = generate(tmp_path, managed_tools=["claude-code"])
    assert code == 0
    top = {p.split("/")[0] for p in tree(out)}
    assert top == {"briefcase", "claude-code", "README.md", "manifest.json", ".briefcase-pack"}


def test_refusals(home, tmp_path, capsys):
    assert managed.cmd_managed(args(None)) == 2
    assert "--managed needs --out DIR" in capsys.readouterr().err
    assert managed.cmd_managed(args(tmp_path / "x", managed_tools=["gemini-cli"])) == 2
    assert managed.cmd_managed(args("/etc/briefcase-pack")) == 1
    assert "system path" in capsys.readouterr().err
    bad = tmp_path / "bad.toml"
    bad.write_text("version = 1\nnope = 1\n")
    assert managed.cmd_managed(args(tmp_path / "y", policy=str(bad))) == 1
    assert "not a valid policy" in capsys.readouterr().err and not (tmp_path / "y").exists()
    full = tmp_path / "full"
    full.mkdir()
    (full / "keep.txt").write_text("x")
    assert managed.cmd_managed(args(full)) == 1
    assert "did not write" in capsys.readouterr().err and (full / "keep.txt").read_text() == "x"


def test_a_symlinked_child_of_the_output_directory_is_never_written_through(home, tmp_path, capsys):
    out = tmp_path / "pack"
    elsewhere = tmp_path / "elsewhere"
    out.mkdir()
    elsewhere.mkdir()
    (out / "claude-code").symlink_to(elsewhere)
    assert managed.cmd_managed(args(out, force=True)) == 1
    assert "symlink" in capsys.readouterr().err
    assert list(elsewhere.iterdir()) == []


def test_dry_run_writes_nothing(home, tmp_path):
    out = tmp_path / "dry"
    lines = []
    assert managed.cmd_managed(args(out, dry_run=True), out=lines.append) == 0
    assert not out.exists() and "Dry run" in lines[-1]


def test_connect_managed_through_the_cli(home, tmp_path, capsys):
    from briefcase.cli.__main__ import main
    out = tmp_path / "cli-pack"
    assert main(["connect", "--managed", "--out", str(out), "--tools", "cursor", "codex"]) == 0
    assert (out / "README.md").exists() and (out / "cursor" / "hooks.json").exists()
    assert not (home / ".cursor").exists() and not (home / ".briefcase" / "connect").exists()
    assert "Next: read" in capsys.readouterr().out


def test_a_pack_directory_is_only_replaced_when_every_file_in_it_is_one_a_pack_wrote(home, tmp_path, capsys):
    out = tmp_path / "pack"
    assert managed.cmd_managed(args(out)) == 0
    assert managed.cmd_managed(args(out)) == 1 and "earlier rollout pack" in capsys.readouterr().err
    assert managed.cmd_managed(args(out, force=True)) == 0  # every file is ours by hash
    (out / "README.md").write_text("mine now")  # edited: no longer ours
    assert managed.cmd_managed(args(out, force=True)) == 1
    assert (out / "README.md").read_text() == "mine now"
    assert managed.cmd_managed(args(out, force=True, connect_dry_run=True)) == 1  # a dry run says the same
    here = tmp_path / "here"
    here.mkdir()
    (here / "README.md").write_text("unrelated")
    (here / "manifest.json").write_text("{}")
    assert managed.cmd_managed(args(here, force=True)) == 1
    assert (here / "README.md").read_text() == "unrelated" and (here / "manifest.json").read_text() == "{}"


def test_pack_files_with_supplied_credentials_are_private_and_the_endpoint_is_shell_quoted(home, tmp_path):
    out = tmp_path / "pack"
    hostile = "http://x.example.com/$(touch pwned)'"
    assert managed.cmd_managed(args(out, headers=["Authorization=Bearer SUPPLIED"], endpoint=hostile)) == 0
    modes = {p.name: p.stat().st_mode & 0o777 for p in out.rglob("*") if p.is_file()}
    assert set(modes.values()) == {0o600}
    command = json.loads((out / "cursor" / "hooks.json").read_text())["hooks"]["sessionStart"][0]["command"]
    import shlex
    assert shlex.split(command)[-1] == hostile  # one argument: nothing in it is interpreted


def test_regenerating_a_pack_removes_what_the_old_pack_wrote_and_hash_owns_the_manifest(home, tmp_path):
    out = tmp_path / "pack"
    assert managed.cmd_managed(args(out, managed_tools=["claude-code", "codex"],
                                    headers=["Authorization=Bearer SUPPLIED-TOKEN"])) == 0
    assert (out / "codex").is_dir()
    assert managed.cmd_managed(args(out, managed_tools=["claude-code"], force=True)) == 0
    assert not (out / "codex").exists(), "the old pack's Codex files (with its credential) must not be left behind"
    assert managed.cmd_managed(args(out, managed_tools=["claude-code"], force=True)) == 0  # and --force still works
    manifest = (out / "manifest.json")
    manifest.write_text(manifest.read_text().replace("claude-code", "mine", 1))
    assert managed.cmd_managed(args(out, managed_tools=["claude-code"], force=True)) == 1
    assert "mine" in manifest.read_text(), "an edited manifest is the user's now"
