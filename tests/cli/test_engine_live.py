"""Opt-in tests against real things. Each is skipped unless its variable is set.

BRIEFCASE_ENGINE_LIVE_DOCKER=1 — a real Docker daemon (``DOCKER_HOST`` or the default socket):
  an offline install of a generated, signed fixture release (a small but valid OCI image layout
  archive), so ``docker load``, ``docker image inspect`` and ``docker tag`` run for real and the
  loaded image IDs are checked against the signed manifest. Images are removed afterwards. The
  throwaway signing key is pinned from Python, as in the offline tests.

BRIEFCASE_ENGINE_LIVE=1 — the real portal flow: needs a platform (BRIEFCASE_PLATFORM_URL) with
  the engine configured (S3, ECR, KMS), an entitled account's BRIEFCASE_LICENSE_TOKEN, Docker, and
  an SDK build that bundles the production release key. BRIEFCASE_ENGINE_LIVE_UP=1 also starts the
  engine and calls it over gRPC (needs the `engine` extra).

Everything goes into a temporary BRIEFCASE_HOME.
"""
import json
import os
import shutil
import subprocess

import pytest

from briefcase import engine as _engine
from briefcase.cli.__main__ import main
from briefcase.engine import install, trust

from tests.cli.engine_fakes import SigningKey, build_release


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIEFCASE_HOME", str(tmp_path / ".briefcase"))
    for name in ("BRIEFCASE_ENGINE_ADDR", "OCI_JJ_SERVER", "OCI_JJ_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.mark.skipif(os.environ.get("BRIEFCASE_ENGINE_LIVE_DOCKER") != "1",
                    reason="set BRIEFCASE_ENGINE_LIVE_DOCKER=1 with a Docker daemon (see module docstring)")
def test_offline_install_loads_a_real_oci_archive(home, monkeypatch, capsys):
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not found")
    key = SigningKey(home / "signing")
    keys_dir = home / "keys"
    key.pin(keys_dir)
    monkeypatch.setattr(trust, "KEYS_DIR", keys_dir)
    arch = install.host_target()["arch"]
    if arch not in ("x86_64", "aarch64"):
        pytest.skip(f"no fixture bundle for {arch}")
    release = build_release(key, version="0.0.1-live", targets=(), bundle_arch=arch)
    directory = release.write(home / "downloads")
    bundle = directory / f"engine-images-0.0.1-live-{arch}.tar"
    try:
        assert main(["engine", "install", "--yes", "--bundle", str(bundle),
                     "--manifest", str(directory / "engine-manifest.json")]) == 0
        record = _engine.current_install()
        for name, info in release.images.items():
            ref = record["images"][name]
            out = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", ref],
                                 capture_output=True, text=True, check=True).stdout.strip()
            assert out == record["image_ids"][name] and out in (info["config"], info["digest"], info["manifest"])
        assert record["client"]["mode"] == "container"  # this fixture release ships no binary
        capsys.readouterr()
        assert main(["engine", "doctor", "--json"]) in (0, 1)
        checks = {row["check"]: row for row in json.loads(capsys.readouterr().out)}
        assert checks["signature"]["severity"] == "ok" and checks["image server"]["severity"] == "ok"
    finally:
        record = _engine.current_install() or {}
        for ref in list((record.get("images") or {}).values()) + list((record.get("image_ids") or {}).values()):
            subprocess.run(["docker", "image", "rm", "-f", ref], capture_output=True)
        for name in release.images:
            subprocess.run(["docker", "image", "rm", "-f", f"briefcase-engine/{name}:0.0.1-live"],
                           capture_output=True)


live = pytest.mark.skipif(os.environ.get("BRIEFCASE_ENGINE_LIVE") != "1",
                          reason="set BRIEFCASE_ENGINE_LIVE=1 (see module docstring)")


@live
def test_install_status_doctor(home, capsys):
    if not trust.load_keys():
        pytest.skip(trust.NO_KEY_MESSAGE)
    assert main(["engine", "install", "--yes"]) == 0
    capsys.readouterr()
    assert main(["engine", "status", "--json"]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["installed"] and state["key_id"] in {k.key_id for k in trust.load_keys()}
    assert main(["engine", "doctor"]) in (0, 1)
    assert "[ok]   signature" in capsys.readouterr().out


@live
@pytest.mark.skipif(os.environ.get("BRIEFCASE_ENGINE_LIVE_UP") != "1", reason="set BRIEFCASE_ENGINE_LIVE_UP=1")
def test_up_query_down(home, capsys):
    pytest.importorskip("grpc")
    if not trust.load_keys():
        pytest.skip(trust.NO_KEY_MESSAGE)
    assert main(["engine", "install", "--yes"]) == 0
    try:
        assert main(["engine", "up", "--wait"]) == 0
        capsys.readouterr()
        assert main(["env", "bookmark", "list", "--json"]) == 0
        assert "refs" in json.loads(capsys.readouterr().out)
        assert main(["graph", "log", os.environ.get("OCI_JJ_REPO", "local/rl-gym-env")]) == 0
    finally:
        main(["engine", "down", "--volumes", "--yes"])
