"""Tests for ArtifactLineageClient (briefcase.integrations.lakefs.lineage)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from briefcase.integrations.lakefs.lineage import (
    ArtifactCommitInfo,
    ArtifactLineageClient,
    ArtifactLineageConfig,
    ArtifactLineageError,
    _split_token_candidates,
)


# ---------------------------------------------------------------------------
# Simulated mode
# ---------------------------------------------------------------------------


def test_simulated_mode_generates_commit_and_state(tmp_path: Path) -> None:
    source = tmp_path / "sample.txt"
    source.write_text("hello world", encoding="utf-8")

    client = ArtifactLineageClient(
        ArtifactLineageConfig(
            repository="demo-repo",
            branch="main",
            mode="simulate",
            local_state_dir=tmp_path / "lineage_state",
        )
    )

    commit = client.version_files(
        files={"docs/sample.txt": source},
        message="initial",
        metadata={"event": "test"},
    )

    assert commit.mode == "simulated"
    assert len(commit.commit_id) == 64
    assert commit.repository == "demo-repo"
    assert "docs/sample.txt" in commit.files
    assert (tmp_path / "lineage_state" / "demo-repo__main.json").exists()


def test_simulated_mode_new_commit_on_change(tmp_path: Path) -> None:
    source = tmp_path / "sample.txt"
    source.write_text("v1", encoding="utf-8")

    client = ArtifactLineageClient(
        ArtifactLineageConfig(
            repository="demo-repo",
            branch="main",
            mode="simulate",
            local_state_dir=tmp_path / "lineage_state",
        )
    )

    first = client.version_files(files={"docs/sample.txt": source}, message="v1")
    source.write_text("v2", encoding="utf-8")
    second = client.version_files(files={"docs/sample.txt": source}, message="v2")

    assert first.commit_id != second.commit_id


def test_missing_source_file_raises(tmp_path: Path) -> None:
    client = ArtifactLineageClient(
        ArtifactLineageConfig(
            repository="demo-repo",
            branch="main",
            mode="simulate",
            local_state_dir=tmp_path / "lineage_state",
        )
    )

    with pytest.raises(ArtifactLineageError):
        client.version_files(
            files={"docs/missing.txt": tmp_path / "missing.txt"},
            message="should fail",
        )


def test_object_uri_uses_commit_or_branch(tmp_path: Path) -> None:
    client = ArtifactLineageClient(
        ArtifactLineageConfig(
            repository="demo-repo",
            branch="feature-a",
            mode="simulate",
            local_state_dir=tmp_path / "lineage_state",
        )
    )

    assert client.object_uri("a/b.txt") == "lakefs://demo-repo/feature-a/a/b.txt"
    assert (
        client.object_uri("a/b.txt", "abc123")
        == "lakefs://demo-repo/abc123/a/b.txt"
    )


# ---------------------------------------------------------------------------
# Configuration and mode resolution
# ---------------------------------------------------------------------------


def test_invalid_mode_raises_value_error(tmp_path: Path):
    with pytest.raises(ValueError):
        ArtifactLineageClient(
            ArtifactLineageConfig(repository="repo", mode="unsupported", local_state_dir=tmp_path)
        )


def test_from_env_reads_configuration(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LAKEFS_MODE", "simulate")
    monkeypatch.setenv("LAKEFS_STORAGE_NAMESPACE", "s3://bucket/prefix")
    monkeypatch.setenv("LAKEFS_BASE_URI", "https://lakefs.example.com")
    monkeypatch.setenv("LAKEFS_CONFIG_PATH", "/tmp/lakectl.yaml")

    client = ArtifactLineageClient.from_env("repo", branch="dev", local_state_dir=tmp_path)
    assert client.config.repository == "repo"
    assert client.config.branch == "dev"
    assert client.config.storage_namespace == "s3://bucket/prefix"
    assert client.config.base_uri == "https://lakefs.example.com"
    assert client.config.config_path == "/tmp/lakectl.yaml"


def test_mode_property_variants(monkeypatch, tmp_path: Path):
    sim_client = ArtifactLineageClient(
        ArtifactLineageConfig(repository="repo", mode="simulate", local_state_dir=tmp_path)
    )
    assert sim_client.mode == "simulated"

    live_client = ArtifactLineageClient(
        ArtifactLineageConfig(repository="repo", mode="live", local_state_dir=tmp_path)
    )
    assert live_client.mode == "live"

    auto_client = ArtifactLineageClient(
        ArtifactLineageConfig(repository="repo", mode="auto", local_state_dir=tmp_path)
    )
    monkeypatch.setattr(auto_client, "_can_use_lakefs_live", lambda: True)
    assert auto_client.mode == "live"
    monkeypatch.setattr(auto_client, "_can_use_lakefs_live", lambda: False)
    assert auto_client.mode == "simulated"


# ---------------------------------------------------------------------------
# Live mode (lakectl invocations mocked)
# ---------------------------------------------------------------------------


def test_version_files_live_and_fallback_paths(monkeypatch, tmp_path: Path):
    source = tmp_path / "a.txt"
    source.write_text("hello", encoding="utf-8")

    # auto mode: live failure falls back to simulated
    auto_client = ArtifactLineageClient(
        ArtifactLineageConfig(repository="repo", mode="auto", local_state_dir=tmp_path / "state-auto")
    )
    monkeypatch.setattr(auto_client, "_can_use_lakefs_live", lambda: True)
    monkeypatch.setattr(
        auto_client,
        "_version_files_live",
        MagicMock(side_effect=ArtifactLineageError("live fail")),
    )
    result = auto_client.version_files({"docs/a.txt": source}, "msg")
    assert result.mode == "simulated"

    # live mode: live failure raises
    strict_live = ArtifactLineageClient(
        ArtifactLineageConfig(repository="repo", mode="live", local_state_dir=tmp_path / "state-live")
    )
    monkeypatch.setattr(
        strict_live,
        "_version_files_live",
        MagicMock(side_effect=ArtifactLineageError("live fail")),
    )
    with pytest.raises(ArtifactLineageError):
        strict_live.version_files({"docs/a.txt": source}, "msg")


def test_version_files_live_success_and_head_fallback(monkeypatch, tmp_path: Path):
    source = tmp_path / "live.txt"
    source.write_text("hello", encoding="utf-8")

    client = ArtifactLineageClient(
        ArtifactLineageConfig(repository="repo", branch="feature", mode="live", local_state_dir=tmp_path)
    )

    calls = []

    def fake_run_checked(cmd, ignore_errors=()):
        calls.append((cmd, ignore_errors))
        if cmd[:3] == ["lakectl", "fs", "upload"]:
            return "uploaded"
        if cmd[:2] == ["lakectl", "commit"]:
            # no hash in output to force _head_commit_id_live fallback
            return "commit created"
        if cmd[:2] == ["lakectl", "log"]:
            return "commit 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        return ""

    monkeypatch.setattr(client, "_run_checked", fake_run_checked)
    monkeypatch.setattr(client, "_ensure_repo_and_branch_live", lambda: None)

    info = client.version_files({"docs/live.txt": source}, "commit message", {"a": "b"})
    assert isinstance(info, ArtifactCommitInfo)
    assert info.mode == "live"
    assert len(info.commit_id) == 64
    assert info.files == {"docs/live.txt": str(source)}
    assert any(cmd[0][:3] == ["lakectl", "fs", "upload"] for cmd in calls)


def test_version_files_live_missing_file_raises(tmp_path: Path):
    client = ArtifactLineageClient(
        ArtifactLineageConfig(repository="repo", mode="live", local_state_dir=tmp_path)
    )
    with pytest.raises(ArtifactLineageError):
        client._version_files_live({"docs/missing.txt": tmp_path / "missing.txt"}, "msg", {})


def test_ensure_repo_and_branch_live_calls_expected_commands(monkeypatch, tmp_path: Path):
    client = ArtifactLineageClient(
        ArtifactLineageConfig(
            repository="repo",
            branch="feature",
            mode="live",
            storage_namespace="s3://bucket/prefix",
            local_state_dir=tmp_path,
        )
    )

    calls = []

    def fake_run_checked(cmd, ignore_errors=()):
        calls.append((cmd, ignore_errors))
        return "ok"

    monkeypatch.setattr(client, "_run_checked", fake_run_checked)
    client._ensure_repo_and_branch_live()

    assert len(calls) == 2
    assert calls[0][0][:3] == ["lakectl", "repo", "create"]
    assert calls[1][0][:3] == ["lakectl", "branch", "create"]
    assert calls[0][1] == ("already exists",)
    assert calls[1][1] == ("already exists",)


def test_head_commit_and_live_capability_checks(monkeypatch, tmp_path: Path):
    client = ArtifactLineageClient(
        ArtifactLineageConfig(repository="repo", mode="auto", local_state_dir=tmp_path)
    )

    monkeypatch.setattr(
        client,
        "_run_checked",
        lambda _cmd: "sha 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    )
    assert len(client._head_commit_id_live()) == 64

    # no binary available
    monkeypatch.setattr("shutil.which", lambda _x: None)
    assert client._can_use_lakefs_live() is False

    # binary available + command ok
    monkeypatch.setattr("shutil.which", lambda _x: "/usr/bin/lakectl")
    monkeypatch.setattr(client, "_run_checked", lambda _cmd: "ok")
    assert client._can_use_lakefs_live() is True

    # binary available + command fails
    def raise_lineage(_cmd):
        raise ArtifactLineageError("nope")

    monkeypatch.setattr(client, "_run_checked", raise_lineage)
    assert client._can_use_lakefs_live() is False


def test_lakectl_cmd_and_run_checked_branches(monkeypatch, tmp_path: Path):
    client = ArtifactLineageClient(
        ArtifactLineageConfig(
            repository="repo",
            mode="simulate",
            base_uri="https://lakefs.example.com",
            config_path="/tmp/lakectl.yaml",
            local_state_dir=tmp_path,
        )
    )

    cmd = client._lakectl_cmd(["repo", "list"])
    assert cmd[:5] == [
        "lakectl", "--base-uri", "https://lakefs.example.com", "--config", "/tmp/lakectl.yaml",
    ]

    # success path
    def run_success(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, "out", "err")

    monkeypatch.setattr(subprocess, "run", run_success)
    assert "out" in client._run_checked(["lakectl", "repo", "list"])

    # ignore_errors path
    def run_already_exists(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], output="", stderr="already exists")

    monkeypatch.setattr(subprocess, "run", run_already_exists)
    assert "already exists" in client._run_checked(
        ["lakectl", "repo", "create"], ignore_errors=("already exists",)
    )

    # hard error path
    def run_hard_fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], output="", stderr="permission denied")

    monkeypatch.setattr(subprocess, "run", run_hard_fail)
    with pytest.raises(ArtifactLineageError):
        client._run_checked(["lakectl", "repo", "create"])


# ---------------------------------------------------------------------------
# State file and parsing helpers
# ---------------------------------------------------------------------------


def test_state_file_load_save_extract_and_split_helpers(tmp_path: Path):
    client = ArtifactLineageClient(
        ArtifactLineageConfig(repository="team/repo", branch="dev", mode="simulate", local_state_dir=tmp_path)
    )

    assert client._state_file.name == "team_repo__dev.json"

    # initial state
    state = client._load_state()
    assert state["head_commit"] == ""

    # save + reload state
    new_state = {
        "repository": "team/repo",
        "branch": "dev",
        "head_commit": "abc",
        "commits": [{"commit_id": "abc"}],
    }
    client._save_state(new_state)
    reloaded = client._load_state()
    assert reloaded["head_commit"] == "abc"

    # extract commit id helper
    text = "commit: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    assert len(client._extract_commit_id(text)) == 64
    assert client._extract_commit_id("no hash here") is None

    # split token helper
    tokens = _split_token_candidates("a,b;c\n[d](e):f")
    assert "a" in tokens and "f" in tokens
