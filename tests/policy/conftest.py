"""Every policy test runs in a throwaway HOME with an empty PATH and no system
policy, so no test can read or change the real user's policy or tool settings."""

import os

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    bindir = tmp_path / "bin"
    for directory in (home, project, bindir):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("BRIEFCASE_SYSTEM_POLICY", str(tmp_path / "system" / "policy.toml"))
    for name in ("BRIEFCASE_HOME", "BRIEFCASE_RECEIVER_TOKEN", "BRIEFCASE_RECEIVER_ENDPOINT",
                 "BRIEFCASE_POLICY_REPORT", "CLAUDE_PROJECT_DIR", "CURSOR_PROJECT_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(project)
    return home


@pytest.fixture
def project(home):
    return home.parent / "project"


@pytest.fixture
def write_policy(home):
    """Write a policy file; ``where`` is ``user``, ``project`` or ``system``."""

    def write(text, where="user"):
        if where == "user":
            path = home / ".briefcase" / "policy.toml"
        elif where == "project":
            path = home.parent / "project" / ".briefcase" / "policy.toml"
        else:
            path = home.parent / "system" / "policy.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    return write


@pytest.fixture
def fake_bin(tmp_path):
    def make(name, output="1.0.0"):
        path = tmp_path / "bin" / name
        path.write_text(f"#!/bin/sh\necho '{output}'\n")
        os.chmod(path, 0o755)
        return path

    return make
