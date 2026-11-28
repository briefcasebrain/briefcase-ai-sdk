"""The public CLI cannot provision the licensed engine, including via historical arguments."""
import json

import pytest

from briefcase import engine
from briefcase.cli.__main__ import main


@pytest.mark.parametrize("argv", [
    ["install", "--code", "private-code"], ["install", "--token", "private-token"],
    ["install", "--bundle", "private.tar", "--manifest", "private.json", "--yes"],
    ["status", "--token", "private-token"], ["status", "--token=private-token"],
    ["--token", "private-token"], ["install", "--dry-run"], ["up"], ["up", "--wait"], ["start"], ["upgrade", "--yes"],
])
def test_retired_entrypoints_reject_before_side_effects(argv, shell_home, monkeypatch, capsys):
    from unittest.mock import patch

    monkeypatch.setenv("BRIEFCASE_LICENSE_TOKEN", "private-token")
    with patch("subprocess.Popen", side_effect=AssertionError("unexpected process")), \
            patch("socket.create_connection", side_effect=AssertionError("unexpected network access")), \
            pytest.raises(SystemExit) as rejected:
        main(["engine", *argv])
    assert rejected.value.code == 2
    assert not (shell_home / ".briefcase" / "engine").exists()
    output = capsys.readouterr()
    assert "private-token" not in output.err and "private-code" not in output.err


def test_help_and_discovery_point_to_managed_access(shell_home, capsys):
    with pytest.raises(SystemExit):
        main(["engine", "--help"])
    help_text = capsys.readouterr().out
    assert engine.MANAGED_ACCESS in help_text
    assert all(word in help_text for word in ["status", "down", "uninstall"])
    assert all(word not in help_text for word in ["--code", "--bundle", "--token", "upgrade"])
    assert main(["features", "--json"]) == 0
    groups = json.loads(capsys.readouterr().out)["groups"]
    feature = next(f for group in groups for f in group["features"] if f["id"] == "engine")
    assert feature["command"] == "briefcase-ai engine status"


def test_legacy_server_image_is_never_used_as_a_client(shell_home, tmp_path):
    from unittest.mock import Mock

    from briefcase.cli.engine import OciJJEngine
    from briefcase.engine.install import save_state

    record = {"version": "legacy", "grpc_addr": "http://127.0.0.1:50051",
              "client": {"mode": "container", "image": "proprietary-server", "image_id": "sha256:private"}}
    save_state({"current": "legacy", "installs": {"legacy": record}})
    runner = Mock()
    client = OciJJEngine(binary=None, runner=runner)
    client.exec(client.argv_diff_bench("a", "b"))
    command = runner.call_args.args[0]
    assert command[0] in ("oci-bai", "oci-jj") and "proprietary-server" not in command


@pytest.mark.parametrize("via_flag", [True, False])
def test_managed_push_requires_explicit_registry_even_with_legacy_install(shell_home, monkeypatch, capsys, via_flag):
    from briefcase.engine.install import save_state

    legacy = {"version": "legacy", "tenant": "stale", "gateway": "127.0.0.1:8080"}
    save_state({"current": "legacy", "installs": {"legacy": legacy}})
    prefix = ["--server", "https://managed.example"] if via_flag else []
    if not via_flag:
        monkeypatch.setenv("BRIEFCASE_ENGINE_ADDR", "https://managed.example")
    monkeypatch.delenv("BRIEFCASE_ENGINE_GATEWAY", raising=False)
    assert main(prefix + ["env", "push", "image:v1", "--as", "tenant/image:v1", "--dry-run"]) == 2
    output = capsys.readouterr()
    assert "BRIEFCASE_ENGINE_GATEWAY" in output.err and "127.0.0.1" not in output.out + output.err


@pytest.mark.parametrize("failure", ["login", "push"])
def test_managed_push_failure_points_to_workspace_registry(shell_home, monkeypatch, capsys, failure):
    import subprocess

    from briefcase.engine.install import save_state

    save_state({"current": "old", "installs": {"old": {"tenant": "old", "gateway": "127.0.0.1:8080"}}})
    monkeypatch.setenv("BRIEFCASE_ENGINE_GATEWAY", "registry.example")
    monkeypatch.setenv("OCI_JJ_AUTH_TOKEN", "test-token")

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, int(argv[1] == failure), "", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert main(["--server", "https://managed.example", "env", "push", "app:v1", "--as", "tenant/app:v1"]) != 0
    err = capsys.readouterr().err
    assert "workspace registry" in err and "tenant" in err and "engine logs" not in err and "engine status" not in err
