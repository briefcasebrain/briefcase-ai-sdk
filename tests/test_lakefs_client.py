"""Tests for VersionedClient: strict-by-default init, explicit mock mode,
and raising (never fabricating) on live operation failures."""

import pytest
from unittest.mock import Mock

from briefcase.integrations.lakefs.client import VersionedClient
from briefcase.integrations.lakefs.context import versioned_context
from briefcase.integrations.lakefs.decorators import versioned


@pytest.fixture(autouse=True)
def _clear_lakefs_env(monkeypatch):
    for var in (
        "LAKEFS_ENDPOINT",
        "LAKEFS_ACCESS_KEY",
        "LAKEFS_PRIVATE_KEY",
        "BRIEFCASE_LAKEFS_REQUIRE_LIVE",
    ):
        monkeypatch.delenv(var, raising=False)


def _mock_client():
    return VersionedClient(repository="r", branch="main", mock=True)


class TestStrictInit:
    def test_init_without_credentials_raises(self):
        with pytest.raises(ValueError, match="credentials"):
            VersionedClient(repository="r", branch="main")

    def test_init_with_credentials_but_no_endpoint_raises(self):
        with pytest.raises(ValueError, match="endpoint"):
            VersionedClient(
                repository="r",
                branch="main",
                lakefs_access_key="key",
                lakefs_secret_key="secret",
            )

    def test_init_failure_raises_instead_of_mock(self, monkeypatch):
        import lakefs

        def _boom(*args, **kwargs):
            raise RuntimeError("bad endpoint")

        monkeypatch.setattr(lakefs, "Client", _boom)
        with pytest.raises(RuntimeError):
            VersionedClient(
                repository="r",
                branch="main",
                lakefs_endpoint="https://lakefs.example.com/api/v1",
                lakefs_access_key="key",
                lakefs_secret_key="secret",
            )


class TestExplicitMockMode:
    def test_mock_mode_reads_are_tagged(self):
        client = _mock_client()
        content, metadata = client.read_object("f.txt", return_metadata=True)
        assert content
        assert metadata["mock"] is True
        assert metadata["commit_metadata"]["mock"] is True

    def test_mock_mode_listings_are_tagged(self):
        client = _mock_client()
        entries = client.list_objects("policies/")
        assert entries
        assert all(e["mock"] is True for e in entries)

    def test_mock_mode_conflicts_with_require_live(self):
        with pytest.raises(ValueError, match="require_live"):
            VersionedClient(
                repository="r", branch="main", mock=True, require_live=True
            )

    def test_mock_mode_conflicts_with_require_live_env(self, monkeypatch):
        monkeypatch.setenv("BRIEFCASE_LAKEFS_REQUIRE_LIVE", "1")
        with pytest.raises(ValueError, match="require_live"):
            VersionedClient(repository="r", branch="main", mock=True)


class TestMockPassthrough:
    def test_versioned_context_forwards_mock(self):
        with versioned_context(None, "r", "main", mock=True) as client:
            assert client.mock is True
            _, metadata = client.read_object("f.txt", return_metadata=True)
            assert metadata["mock"] is True

    def test_versioned_decorator_forwards_mock(self):
        @versioned(repository="r", branch="main", mock=True)
        def fn(versioned_client=None):
            return versioned_client.mock

        assert fn(briefcase_client=object()) is True


class TestLiveFailuresRaise:
    @staticmethod
    def _wire_failing_live(client, exc):
        client._has_lakefs = True
        client._lakefs_client = object()
        failing = Mock()
        failing.Repository.return_value.ref.side_effect = exc
        failing.Repository.return_value.branch.side_effect = exc
        client._lakefs = failing

    def test_read_object_raises_on_failure(self):
        client = _mock_client()
        self._wire_failing_live(client, RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            client.read_object("f.txt")

    def test_resolve_latest_commit_raises_on_failure(self):
        client = _mock_client()
        self._wire_failing_live(client, RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            client._resolve_latest_commit()

    def test_fetch_commit_metadata_raises_on_failure(self):
        client = _mock_client()
        self._wire_failing_live(client, RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            client._fetch_commit_metadata()

    def test_list_objects_raises_on_failure(self):
        client = _mock_client()
        self._wire_failing_live(client, RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            client.list_objects("prefix/")

    def test_object_exists_raises_on_failure(self):
        client = _mock_client()
        self._wire_failing_live(client, RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            client.object_exists("f.txt")
