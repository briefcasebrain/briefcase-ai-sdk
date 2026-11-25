"""Live-path and fallback-branch tests for the VCS provider clients.

Provider SDKs are stubbed into ``sys.modules`` (monkeypatch/patch.dict) so
the live branches run without any provider installed.
"""

from __future__ import annotations

import builtins
import importlib
import json
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


def _fake_dvc_modules():
    dvc_module = types.ModuleType("dvc")
    dvc_repo_module = types.ModuleType("dvc.repo")

    class FakeRepo:
        def __init__(self, repo_path):
            self.repo_path = repo_path

    dvc_repo_module.Repo = FakeRepo
    dvc_module.repo = dvc_repo_module
    return {"dvc": dvc_module, "dvc.repo": dvc_repo_module}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "seed")


def test_dvc_live_branches_and_error_paths(tmp_path: Path):
    with patch.dict(sys.modules, _fake_dvc_modules()):
        client = DvcClient(repository="repo", repo_path=str(tmp_path))

    assert client._has_provider is True
    assert client.mock is False

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

    with patch("subprocess.run", side_effect=RuntimeError("git down")):
        with pytest.raises(RuntimeError):
            client._create_version_impl("msg", None)


def test_dvc_missing_sdk_raises_import_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "dvc", None)
    with pytest.raises(ImportError, match=r"briefcase-ai\[vcs-dvc\]"):
        DvcClient(repository="repo")
    # Explicit mock still works without the SDK.
    client = DvcClient(repository="repo", mock=True)
    result = client.create_version("m")
    assert result["mock"] is True
    assert client.capture_metadata()["mock"] is True


@pytest.mark.parametrize("kind", ["dvc", "gitlfs"])
def test_git_backed_create_version_real_repo(tmp_path: Path, kind: str):
    """A real commit is made, scoped to the written paths, with honest errors."""
    from briefcase.integrations.vcs.base import NothingToCommitError

    _init_repo(tmp_path)
    if kind == "dvc":
        with patch.dict(sys.modules, _fake_dvc_modules()):
            client = DvcClient(repository="repo", repo_path=str(tmp_path))
    else:
        (tmp_path / ".gitattributes").write_text("*.bin filter=lfs\n", encoding="utf-8")
        ok = subprocess.CompletedProcess(["git", "lfs", "version"], 0, "ok", "")
        with patch("subprocess.run", return_value=ok):
            client = GitLFSClient(repository="repo", repo_path=str(tmp_path))
        assert client._has_provider is True

    seed_sha = _git(tmp_path, "rev-parse", "HEAD")

    # Nothing was written through the client: refuse rather than `git add -A`.
    (tmp_path / "unrelated.txt").write_text("secret", encoding="utf-8")
    with pytest.raises(ValueError, match="no paths to commit"):
        client.create_version("nothing")
    assert _git(tmp_path, "rev-parse", "HEAD") == seed_sha

    client.write_object("data/a.txt", b"one")
    result = client.create_version("add a")
    assert result["status"] == "success"
    new_sha = _git(tmp_path, "rev-parse", "HEAD")
    assert result["version_id"] == new_sha != seed_sha
    committed = _git(tmp_path, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == ["data/a.txt"]
    # The unrelated file was never staged.
    assert "?? unrelated.txt" in _git(tmp_path, "status", "--porcelain")

    # Same bytes again: nothing to commit, and HEAD is not passed off as new.
    client.write_object("data/a.txt", b"one")
    with pytest.raises(NothingToCommitError):
        client.create_version("no change")
    assert client.version == new_sha
    assert _git(tmp_path, "rev-parse", "HEAD") == new_sha


def test_git_commit_failure_raises_instead_of_returning_head(tmp_path: Path):
    from briefcase.integrations.vcs.base import git_commit_version

    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ["git", "commit"]:
            raise subprocess.CalledProcessError(1, args, "", "hook rejected")
        if args[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(args, 0, " M a.txt\n", "")
        return subprocess.CompletedProcess(args, 0, "oldhead\n", "")

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(subprocess.CalledProcessError):
            git_commit_version(str(tmp_path), "msg", paths=["a.txt"], timeout=5)

    assert all(kwargs["check"] is True for _, kwargs in calls)
    assert all(kwargs["timeout"] == 5 for _, kwargs in calls)
    assert all("shell" not in kwargs for _, kwargs in calls)
    assert not any(args[:2] == ["git", "rev-parse"] for args, _ in calls)


def test_git_commit_add_all_and_timeout(tmp_path: Path):
    from briefcase.integrations.vcs.base import git_commit_version

    _init_repo(tmp_path)
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    sha = git_commit_version(str(tmp_path), "all", paths=None)
    assert sha == _git(tmp_path, "rev-parse", "HEAD")

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["git"], 1)):
        with pytest.raises(subprocess.TimeoutExpired):
            git_commit_version(str(tmp_path), "msg")


def test_gitlfs_detect_provider_and_live_paths(tmp_path: Path):
    # git lfs version returncode != 0
    with patch("subprocess.run", return_value=subprocess.CompletedProcess(["git", "lfs", "version"], 1, "", "")):
        client = GitLFSClient(repository="repo", repo_path=str(tmp_path))
        assert client._has_provider is False

    # Not configured and not mock: every operation raises, nothing is invented.
    with pytest.raises(RuntimeError, match="mock=True"):
        client.read_object("model.bin")
    with pytest.raises(RuntimeError, match="mock=True"):
        client.write_object("model.bin", b"x")
    with pytest.raises(RuntimeError, match="mock=True"):
        client.create_version("msg")
    assert client.version is None

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
    lfs_ok = subprocess.CompletedProcess(["git", "lfs", "version"], 0, "ok", "")
    with patch("subprocess.run", return_value=lfs_ok) as run:
        client = GitLFSClient(repository="repo", repo_path=str(tmp_path), command_timeout=7)
    assert client._has_provider is True
    assert run.call_args.kwargs["timeout"] == 7

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

    with patch("subprocess.run", side_effect=RuntimeError("git fail")):
        with pytest.raises(RuntimeError):
            client._create_version_impl("msg", None)


def _assert_not_implemented(client):
    """No live implementation: every operation raises and nothing is invented."""
    with pytest.raises(NotImplementedError, match="read_object"):
        client.read_object("some/path")
    with pytest.raises(NotImplementedError, match="read_object"):
        client.read_object("some/path", return_metadata=True)
    with pytest.raises(NotImplementedError, match="write_object"):
        client.write_object("some/path", b"data")
    with pytest.raises(NotImplementedError, match="create_version"):
        client.create_version("msg")
    assert client.version is None
    assert client.capture_metadata()["mock"] is False


def test_artivc_without_mock_raises_not_implemented():
    _assert_not_implemented(ArtiVCClient(repository="repo"))

    mock_client = ArtiVCClient(repository="repo", mock=True)
    content, metadata = mock_client.read_object("m.pkl", return_metadata=True)
    assert content.startswith(b"Mock ArtiVC artifact:")
    assert metadata["mock"] is True
    assert mock_client.write_object("m.pkl", b"x")["mock"] is True
    assert mock_client.create_version("m")["mock"] is True


class _FakeHTTPResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_nessie_rest_round_trip():
    requests = []

    def fake_urlopen(request, timeout=None):
        body = json.loads(request.data) if request.data else None
        requests.append((request.get_method(), request.full_url, body, dict(request.headers), timeout))
        if request.get_method() == "GET" and "/contents/" in request.full_url:
            return _FakeHTTPResponse({"type": "ICEBERG_TABLE", "metadataLocation": "s3://b/m.json"})
        if request.get_method() == "GET":
            return _FakeHTTPResponse({"type": "BRANCH", "name": "dev", "hash": "aaa111"})
        return _FakeHTTPResponse({"type": "BRANCH", "name": "dev", "hash": "bbb222"})

    client = NessieClient(
        repository="cat", branch="dev", endpoint="http://nessie:19120/api/v1/",
        token="tok", timeout=3,
    )
    assert client._has_provider is True

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        content = client.read_object("db.events")
        assert json.loads(content)["metadataLocation"] == "s3://b/m.json"

        from briefcase.integrations.vcs.base import NothingToCommitError
        with pytest.raises(NothingToCommitError):
            client.create_version("empty")

        table = {"type": "ICEBERG_TABLE", "metadataLocation": "s3://b/m2.json"}
        client.write_object("db.events", json.dumps(table).encode())
        # Staging does not touch the server.
        assert len(requests) == 1
        result = client.create_version("register", metadata={"k": "v"})

    assert result["version_id"] == "bbb222"
    assert "mock" not in result
    method, url, body, headers, timeout = requests[0]
    assert (method, url) == ("GET", "http://nessie:19120/api/v1/contents/db.events?ref=dev")
    assert headers["Authorization"] == "Bearer tok"
    assert timeout == 3
    assert requests[1][:2] == ("GET", "http://nessie:19120/api/v1/trees/tree/dev")
    method, url, body, _, _ = requests[2]
    assert (method, url) == (
        "POST", "http://nessie:19120/api/v1/trees/branch/dev/commit?expectedHash=aaa111"
    )
    assert body == {
        "commitMeta": {"message": "register", "properties": {"k": "v"}},
        "operations": [
            {"type": "PUT", "key": {"elements": ["db", "events"]}, "content": table}
        ],
    }
    assert client._staged == []


def test_nessie_error_paths():
    import urllib.error

    client = NessieClient(repository="cat")
    not_found = urllib.error.HTTPError("http://x", 404, "nf", None, None)
    with patch("urllib.request.urlopen", side_effect=not_found):
        with pytest.raises(FileNotFoundError):
            client.read_object("db.missing")
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
        with pytest.raises(urllib.error.URLError):
            client.read_object("db.events")

    with pytest.raises(ValueError):
        client.write_object("db.events", b"not json")
    with pytest.raises(ValueError):
        client.write_object("db.events", b"[1]")

    # A commit that does not advance the branch is an error, not a version.
    client.write_object("db.events", b'{"type": "ICEBERG_TABLE"}')
    same = _FakeHTTPResponse({"type": "BRANCH", "name": "main", "hash": "same"})
    with patch("urllib.request.urlopen", return_value=same):
        with pytest.raises(RuntimeError, match="new hash"):
            client.create_version("msg")
    assert client.version is None

    with pytest.raises(NotImplementedError, match="v1"):
        NessieClient(repository="cat", api_version="v2").read_object("db.events")
    with pytest.raises(ValueError):
        NessieClient(repository="cat", endpoint="file:///etc")

    mock_client = NessieClient(repository="cat", mock=True)
    assert mock_client.read_object("c.json").startswith(b"Mock Nessie catalog:")
    assert mock_client.create_version("m")["mock"] is True


def test_ducklake_with_sdk_raises_not_implemented(tmp_path: Path):
    duckdb_module = types.ModuleType("duckdb")
    duckdb_module.connect = MagicMock(return_value=object())

    with patch.dict(sys.modules, {"duckdb": duckdb_module}):
        client = DuckLakeClient(repository="repo", db_path=str(tmp_path / "duck.db"))

    assert client._has_provider is True
    duckdb_module.connect.assert_called_once_with(str(tmp_path / "duck.db"))
    _assert_not_implemented(client)


def test_ducklake_missing_sdk_raises_import_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "duckdb", None)

    with pytest.raises(ImportError, match=r"briefcase-ai\[vcs-ducklake\]"):
        DuckLakeClient(repository="repo")

    client = DuckLakeClient(repository="repo", mock=True)
    assert client._has_provider is False
    assert client._read_object_impl("query.sql").startswith(b"Mock DuckLake result:")
    client._write_object_impl("tbl", b"1", "application/octet-stream")
    assert client._create_version_impl("msg", None).startswith("ducklake-")


def test_iceberg_with_sdk_raises_not_implemented(monkeypatch):
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
    _assert_not_implemented(client)

    monkeypatch.setitem(sys.modules, "pyiceberg", None)
    monkeypatch.setitem(sys.modules, "pyiceberg.catalog", None)
    with pytest.raises(ImportError, match=r"briefcase-ai\[vcs-iceberg\]"):
        IcebergClient(repository="repo")
    assert IcebergClient(repository="repo", mock=True).create_version("m")["mock"] is True


def test_pachyderm_with_sdk_raises_not_implemented(monkeypatch):
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
    _assert_not_implemented(client)

    monkeypatch.setitem(sys.modules, "pachyderm_sdk", None)
    with pytest.raises(ImportError, match=r"briefcase-ai\[vcs-pachyderm\]"):
        PachydermClient(repository="repo")
    assert PachydermClient(repository="repo", mock=True).create_version("m")["mock"] is True
