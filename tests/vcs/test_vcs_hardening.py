"""Live-path and fallback-branch tests for the VCS provider clients.

Provider SDKs are stubbed into ``sys.modules`` (monkeypatch/patch.dict) so
the live branches run without any provider installed.
"""

from __future__ import annotations

import builtins
import importlib
import subprocess
import sys
import types
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import briefcase.integrations.vcs.base as vcs_base_module
from briefcase.integrations.vcs.artivc import ArtiVCClient
from briefcase.integrations.vcs.base import VcsClientBase
from briefcase.integrations.vcs.dvc import DvcClient
from briefcase.integrations.vcs.ducklake import DuckLakeClient
from briefcase.integrations.vcs.gitlfs import GitLFSClient
from briefcase.integrations.vcs.iceberg import IcebergClient
from briefcase.integrations.vcs.nessie import NessieClient
from briefcase.integrations.vcs.pachyderm import PachydermClient


class _BadFormat:
    def __format__(self, _spec: str) -> str:
        raise RuntimeError("bad format")


class _BadLen:
    def __len__(self) -> int:
        raise RuntimeError("bad len")


class _FakeSpan:
    def __init__(self, recording: bool = True):
        self._recording = recording
        self.attrs = {}
        self.events = []

    def is_recording(self) -> bool:
        return self._recording

    def set_attribute(self, key, value):
        self.attrs[key] = value

    def add_event(self, name, attributes=None):
        self.events.append((name, attributes or {}))


class _FakeTraceModule:
    def __init__(self, span):
        self._span = span

    def get_current_span(self):
        return self._span


def _reload_vcs_with_blocked_imports(monkeypatch: pytest.MonkeyPatch, blocked_prefixes):
    """Reload briefcase.integrations.vcs while forcing selected imports to fail."""
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        for prefix in blocked_prefixes:
            if name == prefix or name.startswith(prefix + "."):
                raise ImportError(f"blocked import: {name}")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    module_names = [
        "briefcase.integrations.vcs",
        "briefcase.integrations.vcs.dvc",
        "briefcase.integrations.vcs.nessie",
        "briefcase.integrations.vcs.pachyderm",
        "briefcase.integrations.vcs.artivc",
        "briefcase.integrations.vcs.ducklake",
        "briefcase.integrations.vcs.iceberg",
        "briefcase.integrations.vcs.gitlfs",
    ]
    cached_modules = {name: sys.modules.get(name) for name in module_names}

    for name in module_names:
        sys.modules.pop(name, None)

    try:
        return importlib.import_module("briefcase.integrations.vcs")
    finally:
        # Restore original modules to keep interpreter state stable for later tests.
        for name in module_names:
            sys.modules.pop(name, None)
        for name, module in cached_modules.items():
            if module is not None:
                sys.modules[name] = module


def test_vcs_module_import_fallbacks_cover_optional_providers(monkeypatch):
    module = _reload_vcs_with_blocked_imports(
        monkeypatch,
        [
            "briefcase.integrations.vcs.dvc",
            "briefcase.integrations.vcs.nessie",
            "briefcase.integrations.vcs.pachyderm",
            "briefcase.integrations.vcs.artivc",
            "briefcase.integrations.vcs.ducklake",
            "briefcase.integrations.vcs.iceberg",
            "briefcase.integrations.vcs.gitlfs",
        ],
    )
    assert module.__all__ == ["VcsClientBase"]


def test_vcs_base_not_implemented_methods_raise():
    client = VcsClientBase(provider_type="x", repository="repo")
    with pytest.raises(NotImplementedError):
        client._read_object_impl("a")
    with pytest.raises(NotImplementedError):
        client._write_object_impl("a", b"b", "application/octet-stream")
    with pytest.raises(NotImplementedError):
        client._create_version_impl("msg", None)


def test_vcs_base_instrumentation_branches(monkeypatch):
    client = VcsClientBase(provider_type="x", repository="repo", branch="main")
    client.version = "abc"
    now = datetime.now()

    # HAS_OTEL=False early returns
    monkeypatch.setattr(vcs_base_module, "HAS_OTEL", False)
    client._record_access("p", {}, now)
    client._record_write("p", {}, now)
    client._record_version_creation({}, now)

    # current span missing / not recording
    monkeypatch.setattr(vcs_base_module, "HAS_OTEL", True)
    monkeypatch.setattr(vcs_base_module, "trace", _FakeTraceModule(None))
    client._record_access("p", {}, now)

    not_recording = _FakeSpan(recording=False)
    monkeypatch.setattr(vcs_base_module, "trace", _FakeTraceModule(not_recording))
    client._record_write("p", {}, now)

    # Recording span gets attributes and events
    recording = _FakeSpan(recording=True)
    monkeypatch.setattr(vcs_base_module, "trace", _FakeTraceModule(recording))
    client._record_access("docs/a.txt", {"size": 10}, now)
    client._record_write("docs/a.txt", {"size": 10, "status": "success"}, now)
    client._record_version_creation({"version_id": "v1", "message": "m", "status": "success"}, now)
    assert recording.attrs["vcs.provider"] == "x"
    assert any(name == "x.file_accessed" for name, _ in recording.events)

    # Span failures never propagate
    class BadSpan(_FakeSpan):
        def add_event(self, _name, attributes=None):
            raise RuntimeError("event fail")

    bad = BadSpan(recording=True)
    monkeypatch.setattr(vcs_base_module, "trace", _FakeTraceModule(bad))
    client._record_access("x", {}, now)
    client._record_write("x", {}, now)
    client._record_version_creation({}, now)


def test_dvc_live_branches_and_error_paths(tmp_path: Path):
    dvc_module = types.ModuleType("dvc")
    dvc_repo_module = types.ModuleType("dvc.repo")

    class FakeRepo:
        def __init__(self, repo_path):
            self.repo_path = repo_path

    dvc_repo_module.Repo = FakeRepo
    dvc_module.repo = dvc_repo_module

    with patch.dict(sys.modules, {"dvc": dvc_module, "dvc.repo": dvc_repo_module}):
        client = DvcClient(repository="repo", repo_path=str(tmp_path))

    assert client._has_provider is True

    input_file = tmp_path / "in.bin"
    input_file.write_bytes(b"abc")
    assert client._read_object_impl("in.bin") == b"abc"

    with pytest.raises(FileNotFoundError):
        client._read_object_impl("missing.bin")

    client._write_object_impl("nested/out.bin", b"xyz", "application/octet-stream")
    assert (tmp_path / "nested" / "out.bin").read_bytes() == b"xyz"

    with patch("builtins.open", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            client._write_object_impl("nested/fail.bin", b"xyz", "application/octet-stream")

    def fake_run(args, **kwargs):
        if args[:2] == ["git", "add"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["git", "commit"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "deadbeef\n", "")
        raise AssertionError(f"unexpected command: {args}")

    with patch("subprocess.run", side_effect=fake_run):
        assert client._create_version_impl("msg", None) == "deadbeef"

    with patch("subprocess.run", side_effect=RuntimeError("git down")):
        with pytest.raises(RuntimeError):
            client._create_version_impl("msg", None)


def test_gitlfs_detect_provider_and_live_paths(tmp_path: Path):
    # git lfs version returncode != 0
    with patch("subprocess.run", return_value=subprocess.CompletedProcess(["git", "lfs", "version"], 1, "", "")):
        client = GitLFSClient(repository="repo", repo_path=str(tmp_path))
        assert client._has_provider is False

    # git command raises
    with patch("subprocess.run", side_effect=RuntimeError("git missing")):
        client = GitLFSClient(repository="repo", repo_path=str(tmp_path))
        assert client._has_provider is False

    # .gitattributes missing
    with patch("subprocess.run", return_value=subprocess.CompletedProcess(["git", "lfs", "version"], 0, "ok", "")):
        client = GitLFSClient(repository="repo", repo_path=str(tmp_path))
        assert client._has_provider is False

    # .gitattributes unreadable
    gitattributes = tmp_path / ".gitattributes"
    gitattributes.write_text("filter=lfs", encoding="utf-8")
    with patch("subprocess.run", return_value=subprocess.CompletedProcess(["git", "lfs", "version"], 0, "ok", "")):
        with patch("builtins.open", side_effect=PermissionError("denied")):
            client = GitLFSClient(repository="repo", repo_path=str(tmp_path))
            assert client._has_provider is False

    # configured live branch
    with patch("subprocess.run", return_value=subprocess.CompletedProcess(["git", "lfs", "version"], 0, "ok", "")):
        client = GitLFSClient(repository="repo", repo_path=str(tmp_path))
    assert client._has_provider is True

    payload = tmp_path / "model.bin"
    payload.write_bytes(b"model")
    assert client._read_object_impl("model.bin") == b"model"
    with pytest.raises(FileNotFoundError):
        client._read_object_impl("missing.bin")

    client._write_object_impl("dir/out.bin", b"abc", "application/octet-stream")
    assert (tmp_path / "dir" / "out.bin").read_bytes() == b"abc"

    with patch("builtins.open", side_effect=OSError("io fail")):
        with pytest.raises(OSError):
            client._write_object_impl("dir/fail.bin", b"abc", "application/octet-stream")

    def fake_run(args, **kwargs):
        if args[:2] == ["git", "add"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["git", "commit"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "cafebabe\n", "")
        raise AssertionError(f"unexpected command: {args}")

    with patch("subprocess.run", side_effect=fake_run):
        assert client._create_version_impl("msg", None) == "cafebabe"

    with patch("subprocess.run", side_effect=RuntimeError("git fail")):
        with pytest.raises(RuntimeError):
            client._create_version_impl("msg", None)


def test_artivc_live_and_error_paths():
    client = ArtiVCClient(repository="repo")
    client._has_provider = True

    assert client._read_object_impl("models/m.pkl").startswith(b"ArtiVC artifact:")
    client._write_object_impl("models/m.pkl", b"data", "application/octet-stream")
    assert client._create_version_impl("hello", None) == "artivc-5"

    with pytest.raises(RuntimeError):
        client._read_object_impl(_BadFormat())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        client._write_object_impl("path", _BadLen(), "application/octet-stream")  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        client._create_version_impl(_BadLen(), None)  # type: ignore[arg-type]


def test_nessie_live_and_error_paths():
    client = NessieClient(repository="repo")
    assert client._has_provider is False

    client._has_provider = True
    assert client._read_object_impl("catalog.json").startswith(b"Nessie catalog metadata")
    client._write_object_impl("catalog.json", b"{}", "application/json")
    assert client._create_version_impl("msg", None).startswith("nessie-commit-")

    with pytest.raises(RuntimeError):
        client._read_object_impl(_BadFormat())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        client._write_object_impl("catalog.json", _BadLen(), "application/json")  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        client._create_version_impl(_BadLen(), None)  # type: ignore[arg-type]


def test_ducklake_live_initialization_and_error_paths(tmp_path: Path):
    duckdb_module = types.ModuleType("duckdb")
    duckdb_module.connect = MagicMock(return_value=object())

    with patch.dict(sys.modules, {"duckdb": duckdb_module}):
        client = DuckLakeClient(repository="repo", db_path=str(tmp_path / "duck.db"))

    assert client._has_provider is True
    duckdb_module.connect.assert_called_once_with(str(tmp_path / "duck.db"))
    assert client._read_object_impl("query.sql").startswith(b"DuckLake query result")
    client._write_object_impl("tbl", b"1", "application/octet-stream")
    assert client._create_version_impl("refresh", None).startswith("ducklake-snapshot-")

    with pytest.raises(RuntimeError):
        client._read_object_impl(_BadFormat())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        client._write_object_impl("tbl", _BadLen(), "application/octet-stream")  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        client._create_version_impl(_BadLen(), None)  # type: ignore[arg-type]


def test_ducklake_mock_mode_when_duckdb_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "duckdb", None)

    client = DuckLakeClient(repository="repo")
    assert client._has_provider is False

    assert client._read_object_impl("query.sql").startswith(b"Mock DuckLake result:")
    client._write_object_impl("tbl", b"1", "application/octet-stream")
    assert client._create_version_impl("msg", None).startswith("ducklake-")


def test_iceberg_live_initialization_and_error_paths():
    pyiceberg_module = types.ModuleType("pyiceberg")
    catalog_module = types.ModuleType("pyiceberg.catalog")
    catalog_module.load_catalog = MagicMock(return_value=object())
    pyiceberg_module.catalog = catalog_module

    with patch.dict(sys.modules, {"pyiceberg": pyiceberg_module, "pyiceberg.catalog": catalog_module}):
        client = IcebergClient(repository="repo")

    assert client._has_provider is True
    catalog_module.load_catalog.assert_called_once_with(
        "repo",
        uri=client.endpoint,
        warehouse=client.warehouse,
    )
    assert client._read_object_impl("tbl").startswith(b"Iceberg table")
    client._write_object_impl("tbl", b"1", "application/octet-stream")
    assert client._create_version_impl("refresh", None).startswith("iceberg-snapshot-")

    with pytest.raises(RuntimeError):
        client._read_object_impl(_BadFormat())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        client._write_object_impl("tbl", _BadLen(), "application/octet-stream")  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        client._create_version_impl(_BadLen(), None)  # type: ignore[arg-type]


def test_pachyderm_live_initialization_and_error_paths():
    pach_module = types.ModuleType("pachyderm_sdk")

    class FakePachClient:
        def __init__(self, address):
            self.address = address

        @classmethod
        def from_pachd_address(cls, address):
            return cls(address)

    pach_module.Client = FakePachClient

    with patch.dict(sys.modules, {"pachyderm_sdk": pach_module}):
        client = PachydermClient(repository="repo")

    assert client._has_provider is True
    assert client._provider_client.address == client.endpoint
    assert client._read_object_impl("data.txt").startswith(b"Pachyderm file:")
    client._write_object_impl("data.txt", b"x", "application/octet-stream")
    assert client._create_version_impl("msg", None).startswith("pach-commit-")

    with pytest.raises(RuntimeError):
        client._read_object_impl(_BadFormat())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        client._write_object_impl("data.txt", _BadLen(), "application/octet-stream")  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        client._create_version_impl(_BadLen(), None)  # type: ignore[arg-type]
