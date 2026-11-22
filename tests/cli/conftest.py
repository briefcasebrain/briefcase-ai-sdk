"""Fixtures for the CLI shell tests (discovery, status, features, setup, completion)."""

import os

import pytest


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
        if name.startswith(("BRIEFCASE_", "OTEL_")) or name in ("NO_COLOR", "TERM"):
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
