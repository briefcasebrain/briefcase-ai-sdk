"""Fixtures for the CLI shell tests (discovery, status, features, setup, completion)."""

import os

import pytest

from briefcase.engine import install, trust

from tests.cli.engine_fakes import (
    VERSION, FakePlatform, SigningKey, build_index, build_release, install_fake_tools,
)

TOOLS = ("docker", "skopeo", "xattr")


@pytest.fixture
def shell_home(tmp_path, monkeypatch):
    """A throwaway HOME with an empty PATH, no briefcase environment and no reachable
    receiver, so no test reads or writes the real user's configuration."""
    home = tmp_path / "home"
    for directory in (home, tmp_path / "project", tmp_path / "bin"):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    for name in list(os.environ):
        if name.startswith(("BRIEFCASE_", "OTEL_")) or name in (
                "NO_COLOR", "TERM", "XDG_CONFIG_HOME", "VSCODE_PORTABLE", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path / "project")

    from briefcase.cli import _ux
    from briefcase.receiver import daemon

    monkeypatch.setattr(daemon, "probe", lambda endpoint, timeout=1.0: None)
    _ux.set_no_input(False)
    _ux.set_debug(False)
    yield home
    _ux.set_no_input(False)
    _ux.set_debug(False)


@pytest.fixture
def fake_tool(tmp_path):
    """Put an executable named ``name`` on the (otherwise empty) PATH."""

    def make(name, output="1.0.0"):
        path = tmp_path / "bin" / name
        path.write_text(f"#!/bin/sh\necho '{output}'\n")
        os.chmod(path, 0o755)
        return path

    return make


@pytest.fixture
def key(tmp_path_factory):
    return SigningKey(tmp_path_factory.mktemp("signing"))


@pytest.fixture
def pinned(key, tmp_path, monkeypatch):
    """Test-only trust root: the throwaway key is the one release key this 'SDK build' pins."""
    keys_dir = tmp_path / "keys"
    key.pin(keys_dir)
    monkeypatch.setattr(trust, "KEYS_DIR", keys_dir)
    return keys_dir


@pytest.fixture
def env(shell_home, tmp_path, monkeypatch, key, pinned):
    release = build_release(key)
    platform = FakePlatform(release, build_index(key, [VERSION, "0.3.0"]))
    monkeypatch.setattr(install, "host_target", lambda: {"os": "linux", "arch": "x86_64"})
    monkeypatch.setattr(install, "is_macos", lambda: False)
    log = tmp_path / "docker.log"
    # Without `cryptography` the installer falls back to `cosign verify-blob`; the fake cosign
    # verifies with openssl, so the whole suite also runs (and checks for real) on that path.
    install_fake_tools(tmp_path / "bin", log, monkeypatch, TOOLS + (() if trust.crypto_available() else ("cosign",)))
    monkeypatch.setenv("BRIEFCASE_PLATFORM_URL", platform.url)
    for name in ("OCI_JJ_SERVER", "OCI_JJ_AUTH_TOKEN", "OCI_JJ_TLS_CA", "OCI_JJ_REPO", "BRIEFCASE_LICENSE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    yield {"platform": platform, "release": release, "log": log, "home": shell_home / ".briefcase",
           "key": key, "tmp": tmp_path}
    platform.close()
