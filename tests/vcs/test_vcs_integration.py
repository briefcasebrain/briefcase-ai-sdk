"""Tests for VCS integration: VcsClientBase, provider clients, metadata tracking.

Provider SDKs (dvc, duckdb, pyiceberg, pachyderm-sdk) are optional and not
installed in CI. Tests that assert mock-mode behavior pin the SDKs absent
by installing ``None`` sys.modules entries with monkeypatch, so they hold
even on machines where an SDK happens to be installed.
"""

from __future__ import annotations

import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from briefcase.integrations.vcs.base import VcsClientBase
from briefcase.integrations.vcs.dvc import DvcClient
from briefcase.integrations.vcs.nessie import NessieClient
from briefcase.integrations.vcs.pachyderm import PachydermClient
from briefcase.integrations.vcs.artivc import ArtiVCClient
from briefcase.integrations.vcs.ducklake import DuckLakeClient
from briefcase.integrations.vcs.iceberg import IcebergClient
from briefcase.integrations.vcs.gitlfs import GitLFSClient


@pytest.fixture
def no_providers(monkeypatch):
    """Force provider SDK imports to fail so clients run in mock mode."""
    for name in ("dvc", "duckdb", "pyiceberg", "pachyderm_sdk"):
        monkeypatch.setitem(sys.modules, name, None)


@pytest.fixture
def mock_briefcase_client():
    """Mock briefcase client with config."""
    client = MagicMock()
    client.config = {"vcs_endpoint": "https://vcs.test.com"}
    return client


# =========================================================================
# VcsClientBase
# =========================================================================

class TestVcsClientBaseInit:
    """VcsClientBase initialization."""

    def test_base_client_init_with_params(self):
        client = VcsClientBase(
            provider_type="test",
            repository="test-repo",
            branch="develop",
            endpoint="https://example.com",
            access_key="key123",
            secret_key="secret456",
            token="token789",
        )

        assert client.provider_type == "test"
        assert client.repository == "test-repo"
        assert client.branch == "develop"
        assert client.endpoint == "https://example.com"
        assert client.access_key == "key123"
        assert client.secret_key == "secret456"
        assert client.token == "token789"

    def test_base_client_init_defaults(self):
        client = VcsClientBase(provider_type="test", repository="test-repo")

        assert client.branch == "main"
        assert client.briefcase_client is None
        assert client.endpoint is None
        assert client.version is None
        assert client._version_metadata == {}

    def test_base_client_extra_params_stored(self):
        client = VcsClientBase(
            provider_type="test",
            repository="test-repo",
            custom_param="custom_value",
            another_param="another_value",
        )

        assert client.extra["custom_param"] == "custom_value"
        assert client.extra["another_param"] == "another_value"

    def test_base_client_with_briefcase_client(self, mock_briefcase_client):
        client = VcsClientBase(
            provider_type="test",
            repository="test-repo",
            briefcase_client=mock_briefcase_client,
        )

        assert client.briefcase_client is mock_briefcase_client


class TestVcsClientBaseMetadata:
    """VcsClientBase metadata capture."""

    def test_capture_metadata_returns_dict(self):
        client = VcsClientBase(
            provider_type="test",
            repository="test-repo",
            branch="develop",
            endpoint="https://example.com",
        )

        metadata = client.capture_metadata()

        assert metadata["provider_type"] == "test"
        assert metadata["endpoint"] == "https://example.com"
        assert metadata["repository"] == "test-repo"
        assert metadata["branch"] == "develop"
        assert metadata["version"] is None
        assert "timestamp" in metadata
        assert "version_metadata" in metadata

    def test_capture_metadata_with_version(self):
        client = VcsClientBase(provider_type="test", repository="test-repo")
        client.version = "abc123def456"

        assert client.capture_metadata()["version"] == "abc123def456"


class TestVcsClientBaseContextManager:
    """VcsClientBase context manager protocol."""

    def test_context_manager_enter_returns_self(self):
        client = VcsClientBase(provider_type="test", repository="test-repo")
        with client as ctx:
            assert ctx is client

    def test_context_manager_exit_calls_close(self):
        client = VcsClientBase(provider_type="test", repository="test-repo")
        with patch.object(client, "close") as mock_close:
            with client:
                pass
            mock_close.assert_called_once()

    def test_context_manager_exit_returns_false(self):
        client = VcsClientBase(provider_type="test", repository="test-repo")
        with patch.object(client, "close"):
            assert client.__exit__(None, None, None) is False

    def test_context_manager_with_exception(self):
        client = VcsClientBase(provider_type="test", repository="test-repo")
        with patch.object(client, "close"):
            assert client.__exit__(ValueError, ValueError("test"), None) is False


class TestVcsClientBaseObjectOperations:
    """VcsClientBase read/write operations."""

    def test_read_object_returns_content(self):
        client = VcsClientBase(provider_type="test", repository="test-repo")
        with patch.object(client, "_read_object_impl", return_value=b"content"):
            assert client.read_object("data/file.txt") == b"content"

    def test_read_object_with_metadata_return(self):
        client = VcsClientBase(provider_type="test", repository="test-repo")
        with patch.object(client, "_read_object_impl", return_value=b"test data"):
            content, metadata = client.read_object("data/file.txt", return_metadata=True)

            assert content == b"test data"
            assert metadata["path"] == "data/file.txt"
            assert metadata["provider"] == "test"
            assert metadata["size"] == 9

    def test_read_object_error_recorded(self):
        client = VcsClientBase(provider_type="test", repository="test-repo")
        with patch.object(client, "_read_object_impl", side_effect=FileNotFoundError("Not found")):
            content, metadata = client.read_object("missing.txt", return_metadata=True)

            assert content == b""
            assert metadata["status"] == "error"
            assert "error" in metadata

    def test_write_object_tracks_metadata(self):
        client = VcsClientBase(provider_type="test", repository="test-repo")
        with patch.object(client, "_write_object_impl", return_value=None):
            metadata = client.write_object("data/file.txt", b"content")

            assert metadata["path"] == "data/file.txt"
            assert metadata["status"] == "success"
            assert metadata["size"] == 7

    def test_write_object_with_content_type(self):
        client = VcsClientBase(provider_type="test", repository="test-repo")
        with patch.object(client, "_write_object_impl") as mock_write:
            client.write_object("data/file.csv", b"csv,data", content_type="text/csv")
            mock_write.assert_called_once_with("data/file.csv", b"csv,data", "text/csv")

    def test_write_object_error_recorded(self):
        client = VcsClientBase(provider_type="test", repository="test-repo")
        with patch.object(client, "_write_object_impl", side_effect=IOError("Write failed")):
            metadata = client.write_object("data/file.txt", b"content")

            assert metadata["status"] == "error"
            assert "error" in metadata


class TestVcsClientBaseVersioning:
    """VcsClientBase version creation."""

    def test_create_version_returns_id(self):
        client = VcsClientBase(provider_type="test", repository="test-repo")
        with patch.object(client, "_create_version_impl", return_value="v1.0.0"):
            result = client.create_version("Initial version")

            assert result["version_id"] == "v1.0.0"
            assert result["status"] == "success"

    def test_create_version_updates_internal_state(self):
        client = VcsClientBase(provider_type="test", repository="test-repo")
        with patch.object(client, "_create_version_impl", return_value="sha123"):
            client.create_version("Commit message")

            assert client.version == "sha123"
            assert "version_id" in client._version_metadata

    def test_create_version_with_metadata(self):
        client = VcsClientBase(provider_type="test", repository="test-repo")
        version_meta = {"key": "value"}
        with patch.object(client, "_create_version_impl", return_value="v1"):
            result = client.create_version("Message", metadata=version_meta)
            assert result["metadata"] == version_meta

    def test_create_version_error_recorded(self):
        client = VcsClientBase(provider_type="test", repository="test-repo")
        with patch.object(client, "_create_version_impl", side_effect=RuntimeError("Commit failed")):
            result = client.create_version("Message")

            assert result["status"] == "error"
            assert "error" in result


class TestVcsClientBaseClose:
    """VcsClientBase close method."""

    def test_close_calls_provider_close(self):
        client = VcsClientBase(provider_type="test", repository="test-repo")
        mock_provider = MagicMock()
        client._provider_client = mock_provider

        client.close()

        mock_provider.close.assert_called_once()

    def test_close_handles_missing_close_method(self):
        client = VcsClientBase(provider_type="test", repository="test-repo")
        client._provider_client = MagicMock(spec=[])
        client.close()

    def test_close_handles_exception(self):
        client = VcsClientBase(provider_type="test", repository="test-repo")
        mock_provider = MagicMock()
        mock_provider.close.side_effect = Exception("Close failed")
        client._provider_client = mock_provider
        client.close()


# =========================================================================
# DVC
# =========================================================================

@pytest.mark.usefixtures("no_providers")
class TestDvcClient:
    """DVC provider client in mock mode."""

    def test_dvc_client_init(self):
        client = DvcClient(repository="dvc-repo", branch="develop", repo_path="/path/to/repo")

        assert client.provider_type == "dvc"
        assert client.repository == "dvc-repo"
        assert client.branch == "develop"
        assert client.repo_path == "/path/to/repo"

    def test_dvc_provider_type(self):
        assert DvcClient(repository="test-repo").provider_type == "dvc"

    def test_dvc_capture_metadata(self):
        client = DvcClient(repository="test-repo", branch="main", remote="s3://bucket/dvc")
        metadata = client.capture_metadata()

        assert metadata["provider_type"] == "dvc"
        assert metadata["repository"] == "test-repo"
        assert metadata["branch"] == "main"

    def test_dvc_mock_mode_read(self):
        client = DvcClient(repository="test-repo")
        content = client.read_object("data/train.csv")

        assert isinstance(content, bytes)
        assert b"data/train.csv" in content

    def test_dvc_mock_mode_write(self):
        client = DvcClient(repository="test-repo")
        assert client.write_object("data/out.csv", b"result")["status"] == "success"

    def test_dvc_mock_mode_create_version(self):
        client = DvcClient(repository="test-repo", branch="main")
        result = client.create_version("Updated dataset")

        assert result["status"] == "success"
        assert "dvc" in result["version_id"]
        assert "main" in result["version_id"]


# =========================================================================
# Nessie
# =========================================================================

class TestNessieClient:
    """Nessie provider client (config shell, always mock mode)."""

    def test_nessie_client_init(self):
        client = NessieClient(
            repository="iceberg-catalog",
            branch="develop",
            endpoint="https://nessie.example.com/api/v1",
            token="token123",
        )

        assert client.provider_type == "nessie"
        assert client.repository == "iceberg-catalog"
        assert client.branch == "develop"
        assert client.endpoint == "https://nessie.example.com/api/v1"
        assert client.token == "token123"

    def test_nessie_provider_type(self):
        assert NessieClient(repository="test-repo").provider_type == "nessie"

    def test_nessie_default_endpoint(self):
        assert "localhost:19120" in NessieClient(repository="test-repo").endpoint

    def test_nessie_capture_metadata(self):
        client = NessieClient(repository="test-repo", branch="main", endpoint="https://nessie.test.com")
        metadata = client.capture_metadata()

        assert metadata["provider_type"] == "nessie"
        assert metadata["endpoint"] == "https://nessie.test.com"

    def test_nessie_mock_mode_read(self):
        content = NessieClient(repository="test-repo").read_object("catalog.json")

        assert isinstance(content, bytes)
        assert b"catalog" in content

    def test_nessie_mock_mode_write(self):
        metadata = NessieClient(repository="test-repo").write_object("catalog.json", b"{}")
        assert metadata["status"] == "success"

    def test_nessie_mock_mode_create_version(self):
        result = NessieClient(repository="test-repo", branch="main").create_version("Updated catalog")

        assert result["status"] == "success"
        assert "nessie" in result["version_id"]


# =========================================================================
# Pachyderm
# =========================================================================

@pytest.mark.usefixtures("no_providers")
class TestPachydermClient:
    """Pachyderm provider client in mock mode."""

    def test_pachyderm_client_init(self):
        client = PachydermClient(
            repository="data-repo",
            branch="main",
            endpoint="grpc://localhost:30650",
            token="token123",
        )

        assert client.provider_type == "pachyderm"
        assert client.repository == "data-repo"
        assert client.endpoint == "grpc://localhost:30650"

    def test_pachyderm_provider_type(self):
        assert PachydermClient(repository="test-repo").provider_type == "pachyderm"

    def test_pachyderm_default_endpoint(self):
        assert "30650" in PachydermClient(repository="test-repo").endpoint

    def test_pachyderm_capture_metadata(self):
        client = PachydermClient(repository="test-repo", branch="main", endpoint="grpc://example.com:30650")
        assert client.capture_metadata()["provider_type"] == "pachyderm"

    def test_pachyderm_mock_mode_read(self):
        content = PachydermClient(repository="test-repo").read_object("data/raw.parquet")

        assert isinstance(content, bytes)
        assert b"Pachyderm" in content

    def test_pachyderm_mock_mode_create_version(self):
        result = PachydermClient(repository="test-repo", branch="main").create_version("Data ingestion")

        assert result["status"] == "success"
        assert "pachyderm" in result["version_id"]


# =========================================================================
# ArtiVC
# =========================================================================

class TestArtiVCClient:
    """ArtiVC provider client (config shell, always mock mode)."""

    def test_artivc_client_init(self):
        client = ArtiVCClient(
            repository="ml-models",
            branch="production",
            endpoint="https://artivc.example.com",
            token="token123",
        )

        assert client.provider_type == "artivc"
        assert client.repository == "ml-models"
        assert client.branch == "production"

    def test_artivc_provider_type(self):
        assert ArtiVCClient(repository="test-repo").provider_type == "artivc"

    def test_artivc_default_endpoint(self):
        assert "localhost:8080" in ArtiVCClient(repository="test-repo").endpoint

    def test_artivc_capture_metadata(self):
        client = ArtiVCClient(repository="test-repo", branch="main", endpoint="https://artivc.test.com")
        assert client.capture_metadata()["provider_type"] == "artivc"

    def test_artivc_mock_mode_read(self):
        content = ArtiVCClient(repository="test-repo").read_object("models/v1.pkl")

        assert isinstance(content, bytes)
        assert b"ArtiVC" in content

    def test_artivc_mock_mode_create_version(self):
        result = ArtiVCClient(repository="test-repo", branch="main").create_version("Retrained model")

        assert result["status"] == "success"
        assert "artivc" in result["version_id"]


# =========================================================================
# DuckLake
# =========================================================================

@pytest.mark.usefixtures("no_providers")
class TestDuckLakeClient:
    """DuckLake provider client in mock mode."""

    def test_ducklake_client_init(self):
        client = DuckLakeClient(repository="analytics-lake", branch="main", db_path="/tmp/duckdb.db")

        assert client.provider_type == "ducklake"
        assert client.repository == "analytics-lake"
        assert client.db_path == "/tmp/duckdb.db"

    def test_ducklake_provider_type(self):
        assert DuckLakeClient(repository="test-repo").provider_type == "ducklake"

    def test_ducklake_default_db_path(self):
        assert ":memory:" in DuckLakeClient(repository="test-repo").db_path

    def test_ducklake_capture_metadata(self):
        client = DuckLakeClient(repository="test-repo", branch="main", endpoint="http://lakefs.test.com")
        assert client.capture_metadata()["provider_type"] == "ducklake"

    def test_ducklake_mock_mode_read(self):
        content = DuckLakeClient(repository="test-repo").read_object("queries/report.sql")

        assert isinstance(content, bytes)
        assert b"DuckLake" in content

    def test_ducklake_mock_mode_create_version(self):
        result = DuckLakeClient(repository="test-repo", branch="main").create_version("Daily refresh")

        assert result["status"] == "success"
        assert "ducklake" in result["version_id"]


# =========================================================================
# Iceberg
# =========================================================================

@pytest.mark.usefixtures("no_providers")
class TestIcebergClient:
    """Iceberg provider client in mock mode."""

    def test_iceberg_client_init(self):
        client = IcebergClient(
            repository="data-catalog",
            branch="main",
            endpoint="file:///tmp/warehouse",
            warehouse="/tmp/iceberg-warehouse",
        )

        assert client.provider_type == "iceberg"
        assert client.repository == "data-catalog"
        assert client.warehouse == "/tmp/iceberg-warehouse"

    def test_iceberg_provider_type(self):
        assert IcebergClient(repository="test-repo").provider_type == "iceberg"

    def test_iceberg_default_endpoint(self):
        assert "iceberg-warehouse" in IcebergClient(repository="test-repo").endpoint

    def test_iceberg_capture_metadata(self):
        client = IcebergClient(repository="test-repo", branch="main", endpoint="file:///warehouse")
        assert client.capture_metadata()["provider_type"] == "iceberg"

    def test_iceberg_mock_mode_read(self):
        content = IcebergClient(repository="test-repo").read_object("events.events_table")

        assert isinstance(content, bytes)
        assert b"Iceberg" in content

    def test_iceberg_mock_mode_create_version(self):
        result = IcebergClient(repository="test-repo", branch="main").create_version("Backfilled data")

        assert result["status"] == "success"
        assert "iceberg" in result["version_id"]


# =========================================================================
# Git LFS
# =========================================================================

class TestGitLFSClient:
    """Git LFS provider client. Uses tmp_path so no .gitattributes is found
    and the client stays in mock mode regardless of the host's Git LFS."""

    def test_gitlfs_client_init(self, tmp_path):
        client = GitLFSClient(
            repository="https://github.com/user/ml-datasets",
            branch="main",
            repo_path=str(tmp_path),
        )

        assert client.provider_type == "gitlfs"
        assert client.repository == "https://github.com/user/ml-datasets"
        assert client.repo_path == str(tmp_path)

    def test_gitlfs_provider_type(self, tmp_path):
        assert GitLFSClient(repository="test-repo", repo_path=str(tmp_path)).provider_type == "gitlfs"

    def test_gitlfs_default_repo_path(self):
        assert GitLFSClient(repository="test-repo").repo_path == "."

    def test_gitlfs_capture_metadata(self, tmp_path):
        client = GitLFSClient(
            repository="test-repo",
            branch="main",
            endpoint="https://github.com/repo.git",
            repo_path=str(tmp_path),
        )
        assert client.capture_metadata()["provider_type"] == "gitlfs"

    def test_gitlfs_mock_mode_read(self, tmp_path):
        content = GitLFSClient(repository="test-repo", repo_path=str(tmp_path)).read_object("models/model.pkl")

        assert isinstance(content, bytes)
        assert b"Git LFS" in content

    def test_gitlfs_mock_mode_write(self, tmp_path):
        client = GitLFSClient(repository="test-repo", repo_path=str(tmp_path))
        assert client.write_object("models/out.pkl", b"model_data")["status"] == "success"

    def test_gitlfs_mock_mode_create_version(self, tmp_path):
        result = GitLFSClient(repository="test-repo", branch="main", repo_path=str(tmp_path)).create_version(
            "Updated model"
        )

        assert result["status"] == "success"
        assert "gitlfs" in result["version_id"]


# =========================================================================
# Module imports and exports
# =========================================================================

class TestVcsModuleImports:
    """VCS package imports."""

    def test_vcs_module_imports_base(self):
        from briefcase.integrations.vcs import VcsClientBase as base
        assert base is not None

    def test_vcs_module_imports_dvc(self):
        from briefcase.integrations.vcs import DvcClient as client
        assert client is not None

    def test_vcs_module_imports_nessie(self):
        from briefcase.integrations.vcs import NessieClient as client
        assert client is not None

    def test_vcs_module_imports_pachyderm(self):
        from briefcase.integrations.vcs import PachydermClient as client
        assert client is not None

    def test_vcs_module_imports_artivc(self):
        from briefcase.integrations.vcs import ArtiVCClient as client
        assert client is not None

    def test_vcs_module_imports_ducklake(self):
        from briefcase.integrations.vcs import DuckLakeClient as client
        assert client is not None

    def test_vcs_module_imports_iceberg(self):
        from briefcase.integrations.vcs import IcebergClient as client
        assert client is not None

    def test_vcs_module_imports_gitlfs(self):
        from briefcase.integrations.vcs import GitLFSClient as client
        assert client is not None


class TestVcsModuleAllExports:
    """VCS package __all__ exports."""

    def test_all_clients_in_exports(self):
        from briefcase.integrations.vcs import __all__

        assert "VcsClientBase" in __all__

        import briefcase.integrations.vcs as vcs_module
        for name in (
            "DvcClient",
            "NessieClient",
            "PachydermClient",
            "ArtiVCClient",
            "DuckLakeClient",
            "IcebergClient",
            "GitLFSClient",
        ):
            if hasattr(vcs_module, name):
                assert name in __all__


# =========================================================================
# Mock mode across providers
# =========================================================================

class TestMockMode:
    """VCS clients in mock mode."""

    def test_mock_mode_read_returns_content(self, no_providers):
        content = DvcClient(repository="test-repo").read_object("data/file.csv")

        assert isinstance(content, bytes)
        assert len(content) > 0

    def test_mock_mode_write_succeeds(self):
        metadata = NessieClient(repository="test-repo").write_object("catalog.json", b"data")
        assert metadata["status"] == "success"

    def test_mock_mode_create_version_returns_id(self, no_providers):
        result = PachydermClient(repository="test-repo").create_version("Commit")

        assert result["status"] == "success"
        assert isinstance(result["version_id"], str)
        assert len(result["version_id"]) > 0

    def test_all_providers_mock_mode_compatible(self, no_providers, tmp_path):
        providers = [
            DvcClient(repository="test", repo_path=str(tmp_path)),
            NessieClient(repository="test"),
            PachydermClient(repository="test"),
            ArtiVCClient(repository="test"),
            DuckLakeClient(repository="test"),
            IcebergClient(repository="test"),
            GitLFSClient(repository="test", repo_path=str(tmp_path)),
        ]

        for client in providers:
            content = client.read_object("test.txt")
            assert isinstance(content, bytes)

            metadata = client.write_object("test.txt", b"data")
            assert metadata["status"] == "success"

            version = client.create_version("Test commit")
            assert version["status"] == "success"


# =========================================================================
# Context managers and multi-provider use
# =========================================================================

class TestVcsIntegrationContextManager:
    """VCS clients as context managers."""

    def test_dvc_context_manager(self, no_providers):
        with DvcClient(repository="test-repo") as client:
            assert isinstance(client, DvcClient)
            assert isinstance(client.read_object("data/file.csv"), bytes)

    def test_nessie_context_manager(self):
        with NessieClient(repository="test-repo") as client:
            assert isinstance(client, NessieClient)
            assert client.write_object("catalog.json", b"{}")["status"] == "success"

    def test_gitlfs_context_manager(self, tmp_path):
        with GitLFSClient(repository="test-repo", repo_path=str(tmp_path)) as client:
            assert isinstance(client, GitLFSClient)
            assert client.create_version("Updated model")["status"] == "success"


class TestVcsIntegrationMultipleProviders:
    """Multiple VCS providers together."""

    def test_multiple_providers_independent(self, no_providers):
        dvc = DvcClient(repository="dvc-repo", branch="main")
        nessie = NessieClient(repository="nessie-repo", branch="dev")

        assert b"train.csv" in dvc.read_object("data/train.csv")
        assert b"catalog" in nessie.read_object("catalog.json")

    def test_different_branches_per_provider(self, no_providers):
        client1 = DvcClient(repository="repo1", branch="main")
        client2 = DvcClient(repository="repo1", branch="develop")

        assert client1.capture_metadata()["branch"] == "main"
        assert client2.capture_metadata()["branch"] == "develop"

    def test_sequential_version_creation(self, no_providers):
        dvc = DvcClient(repository="repo", branch="main")
        nessie = NessieClient(repository="repo", branch="main")

        v1 = dvc.create_version("Initial")
        v2 = nessie.create_version("Follow-up")

        assert v1["status"] == "success"
        assert v2["status"] == "success"
        assert v1["version_id"] != v2["version_id"]


class TestVcsInstrumentation:
    """VCS metadata recording."""

    def test_read_object_metadata_contains_all_fields(self, no_providers):
        client = DvcClient(repository="test-repo", branch="main")
        _, metadata = client.read_object("file.txt", return_metadata=True)

        for key in ("path", "provider", "repository", "branch", "size", "status"):
            assert key in metadata

    def test_write_object_metadata_contains_all_fields(self):
        client = NessieClient(repository="test-repo", branch="main")
        metadata = client.write_object("file.txt", b"content")

        for key in ("path", "provider", "repository", "branch", "size", "status"):
            assert key in metadata

    def test_version_metadata_contains_all_fields(self, no_providers):
        client = PachydermClient(repository="test-repo", branch="main")
        result = client.create_version("Commit message", metadata={"key": "value"})

        for key in ("version_id", "provider", "repository", "branch", "message", "timestamp", "metadata", "status"):
            assert key in result

    def test_capture_metadata_timestamp(self, no_providers):
        metadata = DvcClient(repository="test-repo").capture_metadata()

        assert "timestamp" in metadata
        datetime.fromisoformat(metadata["timestamp"])
