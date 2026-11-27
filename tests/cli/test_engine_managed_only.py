"""The public CLI cannot provision the licensed engine, including via historical arguments."""
import json

import pytest

from briefcase import engine
from briefcase.cli.__main__ import main


@pytest.mark.parametrize("argv", [
    ["install", "--code", "private-code"], ["install", "--token", "private-token"],
    ["install", "--bundle", "private.tar", "--manifest", "private.json", "--yes"],
    ["install", "--dry-run"], ["up"], ["up", "--wait"], ["start"], ["upgrade", "--yes"],
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
