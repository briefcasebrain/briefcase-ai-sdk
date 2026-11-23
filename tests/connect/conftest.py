"""Every connect test runs in a throwaway HOME with an empty PATH, so no test
can read or write the real user's tool configuration."""

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
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("PATH", str(bindir))
    for name in ("BRIEFCASE_HOME", "XDG_CONFIG_HOME", "VSCODE_PORTABLE", "VSCODE_APPDATA", "CLAUDE_CONFIG_DIR",
                 "CODEX_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(project)
    return home


@pytest.fixture
def fake_bin(tmp_path):
    """Put an executable named ``name`` on the (otherwise empty) PATH."""

    def make(name, output="1.0.0"):
        path = tmp_path / "bin" / name
        path.write_text(f"#!/bin/sh\necho '{output}'\n")
        os.chmod(path, 0o755)
        return path

    return make
