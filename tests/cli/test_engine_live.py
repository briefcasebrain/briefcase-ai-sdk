"""Opt-in: the real `briefcase engine` flow against a real platform, registry and Docker.

Skipped unless BRIEFCASE_ENGINE_LIVE=1. Needs:

- BRIEFCASE_PLATFORM_URL and BRIEFCASE_LICENSE_TOKEN for an entitled account on a platform
  whose ENGINE_* settings are configured (ENGINE_API.md in the platform repository);
- Docker with the compose plugin;
- optionally BRIEFCASE_ENGINE_LIVE_UP=1 to also start the engine (its images must be real
  oci-jj images, e.g. built locally and served by the operator's registry) and call it over
  gRPC (needs the `engine` extra).

Everything goes into a temporary BRIEFCASE_HOME; images pulled stay in the local Docker store.
"""
import json
import os

import pytest

from briefcase.cli.__main__ import main

pytestmark = pytest.mark.skipif(os.environ.get("BRIEFCASE_ENGINE_LIVE") != "1",
                                reason="set BRIEFCASE_ENGINE_LIVE=1 (see module docstring)")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIEFCASE_HOME", str(tmp_path / ".briefcase"))
    for name in ("BRIEFCASE_ENGINE_ADDR", "OCI_JJ_SERVER", "OCI_JJ_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_install_status_doctor(home, capsys):
    assert main(["engine", "install", "--yes"]) == 0
    capsys.readouterr()
    assert main(["engine", "status", "--json"]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["installed"] and all("@sha256:" in ref for ref in state["install"]["images"].values())
    assert main(["engine", "doctor"]) in (0, 1)
    assert "[ok]   install" in capsys.readouterr().out


@pytest.mark.skipif(os.environ.get("BRIEFCASE_ENGINE_LIVE_UP") != "1", reason="set BRIEFCASE_ENGINE_LIVE_UP=1")
def test_up_query_down(home, capsys):
    pytest.importorskip("grpc")
    assert main(["engine", "install", "--yes"]) == 0
    try:
        assert main(["engine", "up", "--wait"]) == 0
        capsys.readouterr()
        assert main(["env", "bookmark", "list", "--json"]) == 0
        assert "refs" in json.loads(capsys.readouterr().out)
        assert main(["graph", "log", os.environ.get("OCI_JJ_REPO", "local/rl-gym-env")]) == 0
    finally:
        main(["engine", "down", "--volumes", "--yes"])
