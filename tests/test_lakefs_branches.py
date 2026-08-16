"""Tests for BranchManager (briefcase.integrations.lakefs.branches)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from briefcase.integrations.lakefs import (
    BranchInfo,
    BranchManager,
    DiffEntry,
    MergeResult,
    MergeStrategy,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def stub_lakefs_sdk(monkeypatch):
    """Inject a stub lakefs module so tests run without the 'lakefs' extra.

    BranchManager imports lakefs lazily in its constructor; the stub makes
    that import deterministic whether or not the real SDK is installed.
    """
    monkeypatch.setitem(sys.modules, "lakefs", MagicMock())


def make_manager(briefcase_client=None) -> BranchManager:
    """Return a BranchManager in mock mode (no live lakefs client)."""
    return BranchManager(
        repository="test-repo",
        lakefs_client=None,
        briefcase_client=briefcase_client,
    )


# ---------------------------------------------------------------------------
# MergeStrategy enum
# ---------------------------------------------------------------------------


class TestMergeStrategy:
    def test_default_value(self):
        assert MergeStrategy.DEFAULT.value == "default"

    def test_source_wins_value(self):
        assert MergeStrategy.SOURCE_WINS.value == "source-wins"

    def test_dest_wins_value(self):
        assert MergeStrategy.DEST_WINS.value == "dest-wins"

    def test_is_str_enum(self):
        assert isinstance(MergeStrategy.DEFAULT, str)
        assert MergeStrategy.SOURCE_WINS == "source-wins"

    def test_all_members(self):
        members = {m.value for m in MergeStrategy}
        assert members == {"default", "source-wins", "dest-wins"}


# ---------------------------------------------------------------------------
# BranchManager mock mode (no live lakefs client)
# ---------------------------------------------------------------------------


class TestBranchManagerMockMode:
    def test_has_lakefs_false_without_client(self):
        bm = make_manager()
        assert bm._has_lakefs is False

    def test_create_branch_returns_branch_info(self):
        bm = make_manager()
        info = bm.create_branch("feature/test")
        assert isinstance(info, BranchInfo)
        assert info.name == "feature/test"
        assert info.commit_id
        assert info.created_at

    def test_create_branch_with_explicit_source(self):
        bm = make_manager()
        info = bm.create_branch("feature/test", source="develop")
        assert info.name == "feature/test"

    def test_create_branch_uses_default_source_when_none(self):
        bm = BranchManager(
            repository="test-repo",
            lakefs_client=None,
            default_source_branch="staging",
        )
        info = bm.create_branch("feature/x")
        assert info.name == "feature/x"

    def test_delete_branch_no_error_in_mock(self):
        bm = make_manager()
        bm.delete_branch("feature/test")  # should not raise

    def test_merge_returns_merge_result(self):
        bm = make_manager()
        result = bm.merge("feature/test", "main")
        assert isinstance(result, MergeResult)
        assert result.commit_id
        assert isinstance(result.summary, dict)
        assert "added" in result.summary
        assert "removed" in result.summary
        assert "changed" in result.summary

    def test_merge_with_source_wins_strategy(self):
        bm = make_manager()
        result = bm.merge("feature/test", "main", strategy=MergeStrategy.SOURCE_WINS)
        assert isinstance(result, MergeResult)

    def test_merge_with_message(self):
        bm = make_manager()
        result = bm.merge("feature/test", "main", message="Merge agent run")
        assert isinstance(result, MergeResult)

    def test_diff_returns_empty_list_in_mock(self):
        bm = make_manager()
        entries = bm.diff("main", "feature/test")
        assert isinstance(entries, list)
        assert len(entries) == 0

    def test_branch_exists_returns_false_in_mock(self):
        bm = make_manager()
        assert bm.branch_exists("any-branch") is False

    def test_list_branches_returns_empty_in_mock(self):
        bm = make_manager()
        branches = bm.list_branches()
        assert isinstance(branches, list)
        assert len(branches) == 0

    def test_list_branches_with_prefix_returns_empty_in_mock(self):
        bm = make_manager()
        branches = bm.list_branches(prefix="briefcase-staging/")
        assert isinstance(branches, list)

    def test_default_strategy_used_in_merge(self):
        bm = BranchManager(
            repository="test-repo",
            lakefs_client=None,
            default_strategy=MergeStrategy.DEST_WINS,
        )
        # No assertion on the actual merge (mock) but should not raise
        result = bm.merge("src", "dst")
        assert isinstance(result, MergeResult)


# ---------------------------------------------------------------------------
# cleanup_stale_branches
# ---------------------------------------------------------------------------


class TestCleanupStaleBranches:
    def test_deletes_old_staging_branches(self):
        bm = make_manager()
        old_time = (
            datetime.now(tz=timezone.utc) - timedelta(hours=48)
        ).isoformat()
        bm.list_branches = MagicMock(
            return_value=[
                BranchInfo("briefcase-staging/old1", "c1", old_time),
                BranchInfo("briefcase-staging/old2", "c2", old_time),
            ]
        )
        bm.delete_branch = MagicMock()

        deleted = bm.cleanup_stale_branches(max_age_hours=24)

        assert "briefcase-staging/old1" in deleted
        assert "briefcase-staging/old2" in deleted
        assert bm.delete_branch.call_count == 2

    def test_keeps_recent_staging_branches(self):
        bm = make_manager()
        recent_time = datetime.now(tz=timezone.utc).isoformat()
        bm.list_branches = MagicMock(
            return_value=[
                BranchInfo("briefcase-staging/new", "c1", recent_time),
            ]
        )
        bm.delete_branch = MagicMock()

        deleted = bm.cleanup_stale_branches(max_age_hours=24)

        assert deleted == []
        bm.delete_branch.assert_not_called()

    def test_deletes_old_branch_with_naive_datetime(self):
        """Branch timestamp without tzinfo is coerced to UTC before comparison."""
        bm = make_manager()
        naive_old = (
            datetime.now(tz=timezone.utc) - timedelta(hours=48)
        ).replace(tzinfo=None).isoformat()
        bm.list_branches = MagicMock(
            return_value=[
                BranchInfo("briefcase-staging/naive-old", "c1", naive_old),
            ]
        )
        bm.delete_branch = MagicMock()

        deleted = bm.cleanup_stale_branches(max_age_hours=1)
        assert "briefcase-staging/naive-old" in deleted

    def test_returns_empty_when_no_staging_branches(self):
        bm = make_manager()
        bm.list_branches = MagicMock(return_value=[])
        deleted = bm.cleanup_stale_branches()
        assert deleted == []

    def test_handles_invalid_timestamp_gracefully(self):
        bm = make_manager()
        bm.list_branches = MagicMock(
            return_value=[
                BranchInfo("briefcase-staging/bad", "c1", "not-a-date"),
            ]
        )
        bm.delete_branch = MagicMock()
        deleted = bm.cleanup_stale_branches(max_age_hours=1)
        # Should not raise, just skip the bad entry
        assert isinstance(deleted, list)


# ---------------------------------------------------------------------------
# OTel span event emission
# ---------------------------------------------------------------------------


class TestBranchManagerOtelEmission:
    @pytest.fixture
    def mock_span(self):
        span = MagicMock()
        span.is_recording.return_value = True
        return span

    @pytest.fixture
    def mock_briefcase_client(self):
        return MagicMock()

    def test_create_branch_emits_span_event(self, mock_span, mock_briefcase_client):
        with patch("briefcase.integrations.lakefs.branches.HAS_OTEL", True), patch(
            "briefcase.integrations.lakefs.branches.trace"
        ) as mock_trace:
            mock_trace.get_current_span.return_value = mock_span
            bm = BranchManager(
                repository="test-repo",
                lakefs_client=None,
                briefcase_client=mock_briefcase_client,
            )
            bm.create_branch("feature/test")

        mock_span.add_event.assert_called_once()
        event_name = mock_span.add_event.call_args[0][0]
        assert event_name == "lakefs.branch_created"

    def test_merge_emits_span_event(self, mock_span, mock_briefcase_client):
        with patch("briefcase.integrations.lakefs.branches.HAS_OTEL", True), patch(
            "briefcase.integrations.lakefs.branches.trace"
        ) as mock_trace:
            mock_trace.get_current_span.return_value = mock_span
            bm = BranchManager(
                repository="test-repo",
                lakefs_client=None,
                briefcase_client=mock_briefcase_client,
            )
            bm.merge("source", "dest")

        mock_span.add_event.assert_called_once()
        event_name = mock_span.add_event.call_args[0][0]
        assert event_name == "lakefs.branch_merged"

    def test_diff_emits_span_event(self, mock_span, mock_briefcase_client):
        with patch("briefcase.integrations.lakefs.branches.HAS_OTEL", True), patch(
            "briefcase.integrations.lakefs.branches.trace"
        ) as mock_trace:
            mock_trace.get_current_span.return_value = mock_span
            bm = BranchManager(
                repository="test-repo",
                lakefs_client=None,
                briefcase_client=mock_briefcase_client,
            )
            bm.diff("left", "right")

        mock_span.add_event.assert_called_once()
        event_name = mock_span.add_event.call_args[0][0]
        assert event_name == "lakefs.diff_computed"

    def test_no_event_when_no_briefcase_client(self, mock_span):
        with patch("briefcase.integrations.lakefs.branches.HAS_OTEL", True), patch(
            "briefcase.integrations.lakefs.branches.trace"
        ) as mock_trace:
            mock_trace.get_current_span.return_value = mock_span
            bm = BranchManager(
                repository="test-repo",
                lakefs_client=None,
                briefcase_client=None,
            )
            bm.create_branch("test")

        mock_span.add_event.assert_not_called()

    def test_no_event_when_otel_unavailable(self, mock_span, mock_briefcase_client):
        with patch("briefcase.integrations.lakefs.branches.HAS_OTEL", False):
            bm = BranchManager(
                repository="test-repo",
                lakefs_client=None,
                briefcase_client=mock_briefcase_client,
            )
            bm.create_branch("test")

        mock_span.add_event.assert_not_called()


# ---------------------------------------------------------------------------
# DiffEntry, BranchInfo, and MergeResult dataclass contracts
# ---------------------------------------------------------------------------


class TestDataClasses:
    def test_diff_entry_required_fields(self):
        e = DiffEntry(path="data/file.txt", type="added")
        assert e.path == "data/file.txt"
        assert e.type == "added"
        assert e.size_bytes is None

    def test_diff_entry_with_size(self):
        e = DiffEntry(path="model.pkl", type="changed", size_bytes=1024)
        assert e.size_bytes == 1024

    def test_branch_info_fields(self):
        info = BranchInfo(name="main", commit_id="abc123", created_at="2026-01-01T00:00:00+00:00")
        assert info.name == "main"
        assert info.commit_id == "abc123"

    def test_merge_result_fields(self):
        r = MergeResult(commit_id="merged123", summary={"added": 2, "removed": 0, "changed": 1})
        assert r.commit_id == "merged123"
        assert r.summary["added"] == 2


# ---------------------------------------------------------------------------
# Live paths, with the lakeFS SDK mocked to cover the try/except branches
# ---------------------------------------------------------------------------


def _live_manager(briefcase_client=None, default_strategy=MergeStrategy.DEFAULT):
    """Return a BranchManager with _has_lakefs=True backed by a MagicMock lakefs."""
    bm = BranchManager(
        repository="live-repo",
        lakefs_client=None,
        briefcase_client=briefcase_client,
        default_strategy=default_strategy,
    )
    bm._has_lakefs = True
    bm._lakefs = MagicMock()
    return bm


def _mock_repo(bm):
    """Patch _repo() on *bm* to return a fresh MagicMock repository."""
    mock_repo = MagicMock()
    bm._repo = MagicMock(return_value=mock_repo)
    return mock_repo


class TestBranchManagerLivePaths:
    """Cover every live-path try/except block in branches.py."""

    # ---- create_branch ----

    def test_create_branch_live_success(self):
        bm = _live_manager()
        repo = _mock_repo(bm)

        mock_commit = MagicMock()
        mock_commit.id = "live-commit-sha"
        mock_commit.creation_date = 1_700_000_000_000  # epoch ms
        repo.branch.return_value.get_commit.return_value = mock_commit

        info = bm.create_branch("feature/live")
        assert info.name == "feature/live"
        assert info.commit_id == "live-commit-sha"
        repo.branch.return_value.create.assert_called_once_with(source_reference="main")

    def test_create_branch_live_failure_raises_runtime_error(self):
        bm = _live_manager()
        repo = _mock_repo(bm)
        repo.branch.return_value.create.side_effect = Exception("lakeFS error")

        with pytest.raises(RuntimeError, match="Failed to create branch"):
            bm.create_branch("feature/broken")

    # ---- delete_branch ----

    def test_delete_branch_live_success(self):
        bm = _live_manager()
        repo = _mock_repo(bm)
        bm.delete_branch("feature/live")
        repo.branch.return_value.delete.assert_called_once()

    def test_delete_branch_live_failure_raises_runtime_error(self):
        bm = _live_manager()
        repo = _mock_repo(bm)
        repo.branch.return_value.delete.side_effect = Exception("forbidden")

        with pytest.raises(RuntimeError, match="Failed to delete branch"):
            bm.delete_branch("feature/broken")

    # ---- merge ----

    def test_merge_live_success(self):
        bm = _live_manager()
        repo = _mock_repo(bm)

        raw = MagicMock()
        raw.id = "merge-sha"
        raw.summary = {"added": 2, "removed": 0, "changed": 1}
        repo.branch.return_value.merge_into.return_value = raw

        result = bm.merge("src", "dst")
        assert result.commit_id == "merge-sha"
        assert result.summary["added"] == 2

    def test_merge_live_passes_message(self):
        bm = _live_manager()
        repo = _mock_repo(bm)
        raw = MagicMock()
        raw.id = "sha"
        raw.summary = {}
        repo.branch.return_value.merge_into.return_value = raw

        bm.merge("src", "dst", message="hello")
        _, kwargs = repo.branch.return_value.merge_into.call_args
        assert kwargs.get("message") == "hello"

    def test_merge_live_passes_strategy_when_not_default(self):
        bm = _live_manager()
        repo = _mock_repo(bm)
        raw = MagicMock()
        raw.id = "sha"
        raw.summary = {}
        repo.branch.return_value.merge_into.return_value = raw

        bm.merge("src", "dst", strategy=MergeStrategy.SOURCE_WINS)
        _, kwargs = repo.branch.return_value.merge_into.call_args
        assert kwargs.get("strategy") == "source-wins"

    def test_merge_live_does_not_pass_strategy_for_default(self):
        bm = _live_manager()
        repo = _mock_repo(bm)
        raw = MagicMock()
        raw.id = "sha"
        raw.summary = {}
        repo.branch.return_value.merge_into.return_value = raw

        bm.merge("src", "dst")
        _, kwargs = repo.branch.return_value.merge_into.call_args
        assert "strategy" not in kwargs

    def test_merge_live_handles_non_dict_summary(self):
        bm = _live_manager()
        repo = _mock_repo(bm)
        raw = MagicMock()
        raw.id = "sha"
        raw.summary = "not-a-dict"
        repo.branch.return_value.merge_into.return_value = raw

        result = bm.merge("src", "dst")
        assert isinstance(result.summary, dict)

    def test_merge_live_conflict_error(self):
        bm = _live_manager()
        repo = _mock_repo(bm)
        repo.branch.return_value.merge_into.side_effect = Exception("conflict detected")

        with pytest.raises(RuntimeError):
            bm.merge("src", "dst")

    def test_merge_live_non_conflict_error(self):
        bm = _live_manager()
        repo = _mock_repo(bm)
        repo.branch.return_value.merge_into.side_effect = Exception("network timeout")

        with pytest.raises(RuntimeError):
            bm.merge("src", "dst")

    # ---- diff ----

    def _make_diff_item(self, path, raw_type, size=None):
        item = MagicMock()
        item.path = path
        item.type = raw_type
        item.size_bytes = size
        return item

    def test_diff_live_added_type(self):
        bm = _live_manager()
        repo = _mock_repo(bm)
        repo.ref.return_value.diff.return_value = [
            self._make_diff_item("a.txt", "added")
        ]
        entries = bm.diff("main", "feature")
        assert entries[0].type == "added"

    def test_diff_live_removed_type(self):
        bm = _live_manager()
        repo = _mock_repo(bm)
        repo.ref.return_value.diff.return_value = [
            self._make_diff_item("b.txt", "removed")
        ]
        entries = bm.diff("main", "feature")
        assert entries[0].type == "removed"

    def test_diff_live_deleted_normalised_to_removed(self):
        bm = _live_manager()
        repo = _mock_repo(bm)
        repo.ref.return_value.diff.return_value = [
            self._make_diff_item("c.txt", "deleted")
        ]
        entries = bm.diff("main", "feature")
        assert entries[0].type == "removed"

    def test_diff_live_changed_type(self):
        bm = _live_manager()
        repo = _mock_repo(bm)
        repo.ref.return_value.diff.return_value = [
            self._make_diff_item("d.txt", "changed", size=512)
        ]
        entries = bm.diff("main", "feature")
        assert entries[0].type == "changed"
        assert entries[0].size_bytes == 512

    def test_diff_live_mixed_types(self):
        bm = _live_manager()
        repo = _mock_repo(bm)
        repo.ref.return_value.diff.return_value = [
            self._make_diff_item("add.txt", "added"),
            self._make_diff_item("del.txt", "removed"),
            self._make_diff_item("chg.txt", "CHANGED"),  # upper-case normalised
        ]
        entries = bm.diff("main", "feature")
        assert len(entries) == 3
        assert entries[2].type == "changed"

    def test_diff_live_error_returns_empty_list(self):
        bm = _live_manager()
        repo = _mock_repo(bm)
        repo.ref.side_effect = Exception("network error")

        entries = bm.diff("main", "feature")
        assert entries == []

    # ---- branch_exists ----

    def test_branch_exists_live_true(self):
        bm = _live_manager()
        repo = _mock_repo(bm)
        repo.branch.return_value.get_commit.return_value = MagicMock()

        assert bm.branch_exists("main") is True

    def test_branch_exists_live_false_on_exception(self):
        bm = _live_manager()
        repo = _mock_repo(bm)
        repo.branch.return_value.get_commit.side_effect = Exception("not found")

        assert bm.branch_exists("nonexistent") is False

    # ---- list_branches ----

    def test_list_branches_live_success(self):
        bm = _live_manager()
        repo = _mock_repo(bm)

        mock_branch = MagicMock()
        mock_branch.id = "feature/x"
        commit = MagicMock()
        commit.id = "commit-abc"
        commit.creation_date = 1_700_000_000_000
        mock_branch.get_commit.return_value = commit
        repo.branches.return_value = [mock_branch]

        results = bm.list_branches()
        assert len(results) == 1
        assert results[0].name == "feature/x"
        assert results[0].commit_id == "commit-abc"

    def test_list_branches_live_branch_without_get_commit(self):
        bm = _live_manager()
        repo = _mock_repo(bm)

        # A branch object with no get_commit and no id exercises the
        # hasattr fallback, which yields commit_id="unknown"
        mock_branch = MagicMock(spec=["__str__"])
        repo.branches.return_value = [mock_branch]

        results = bm.list_branches()
        assert len(results) == 1
        assert results[0].commit_id == "unknown"

    def test_list_branches_live_inner_exception(self):
        bm = _live_manager()
        repo = _mock_repo(bm)

        mock_branch = MagicMock()
        mock_branch.id = "feature/error"
        mock_branch.get_commit.side_effect = Exception("commit fetch failed")
        repo.branches.return_value = [mock_branch]

        results = bm.list_branches()
        assert len(results) == 1
        assert results[0].commit_id == "unknown"

    def test_list_branches_live_outer_exception(self):
        bm = _live_manager()
        repo = _mock_repo(bm)
        repo.branches.side_effect = Exception("repo error")

        results = bm.list_branches()
        assert results == []


# ---------------------------------------------------------------------------
# _repo() error path
# ---------------------------------------------------------------------------


class TestRepoHelper:
    def test_repo_raises_when_lakefs_is_none(self):
        bm = BranchManager(repository="r", lakefs_client=None)
        bm._has_lakefs = True
        bm._lakefs = None

        with pytest.raises(RuntimeError, match="lakeFS client not initialized"):
            bm._repo()

    def test_repo_raises_when_has_lakefs_false(self):
        bm = BranchManager(repository="r", lakefs_client=None)
        # _has_lakefs is already False in mock mode
        with pytest.raises(RuntimeError, match="lakeFS client not initialized"):
            bm._repo()

    def test_repo_success_returns_repository(self):
        bm = BranchManager(repository="my-repo", lakefs_client=None)
        bm._has_lakefs = True
        mock_lakefs = MagicMock()
        mock_repo_instance = MagicMock()
        mock_lakefs.Repository.return_value = mock_repo_instance
        bm._lakefs = mock_lakefs
        bm._lakefs_client = MagicMock()

        result = bm._repo()
        assert result is mock_repo_instance
        mock_lakefs.Repository.assert_called_once_with("my-repo", client=bm._lakefs_client)


# ---------------------------------------------------------------------------
# _commit_timestamp for all value types
# ---------------------------------------------------------------------------


class TestCommitTimestamp:
    def test_int_epoch_ms(self):
        commit = MagicMock()
        commit.creation_date = 1_700_000_000_000
        ts = BranchManager._commit_timestamp(commit)
        assert "T" in ts  # ISO-8601

    def test_float_epoch_ms(self):
        commit = MagicMock()
        commit.creation_date = 1_700_000_000_000.0
        ts = BranchManager._commit_timestamp(commit)
        assert "T" in ts

    def test_datetime_without_tzinfo(self):
        naive_dt = datetime(2026, 1, 15, 10, 0, 0)
        commit = MagicMock()
        commit.creation_date = naive_dt
        ts = BranchManager._commit_timestamp(commit)
        assert "+00:00" in ts

    def test_datetime_with_tzinfo(self):
        aware_dt = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        commit = MagicMock()
        commit.creation_date = aware_dt
        ts = BranchManager._commit_timestamp(commit)
        assert "2026" in ts

    def test_string_value(self):
        commit = MagicMock()
        commit.creation_date = "2026-01-15T10:00:00+00:00"
        ts = BranchManager._commit_timestamp(commit)
        assert ts == "2026-01-15T10:00:00+00:00"

    def test_fallback_when_all_attrs_none(self):
        commit = MagicMock()
        commit.creation_date = None
        commit.created_at = None
        commit.timestamp = None
        ts = BranchManager._commit_timestamp(commit)
        assert "T" in ts  # still returns a valid ISO-8601 fallback


# ---------------------------------------------------------------------------
# VersionedClient.branch_manager integration
# ---------------------------------------------------------------------------


class TestVersionedClientBranchManager:
    def test_branch_manager_property_returns_manager(self, monkeypatch):
        from briefcase.integrations.lakefs.client import VersionedClient

        monkeypatch.delenv("BRIEFCASE_LAKEFS_REQUIRE_LIVE", raising=False)
        client = VersionedClient(repository="test-repo", branch="main", mock=True)
        bm = client.branch_manager
        assert isinstance(bm, BranchManager)
        assert bm.repository == "test-repo"
        # Mock client has no lakeFS connection, so the manager is in mock mode
        assert bm._has_lakefs is False
        # Cached on repeat access
        assert client.branch_manager is bm


# ---------------------------------------------------------------------------
# _emit_span_event exception path
# ---------------------------------------------------------------------------


class TestEmitSpanEventExceptionPath:
    def test_does_not_propagate_when_span_raises(self):
        mock_span = MagicMock()
        mock_span.is_recording.return_value = True
        mock_span.add_event.side_effect = Exception("tracer exploded")

        with patch("briefcase.integrations.lakefs.branches.HAS_OTEL", True), patch(
            "briefcase.integrations.lakefs.branches.trace"
        ) as mock_trace:
            mock_trace.get_current_span.return_value = mock_span
            bm = BranchManager(
                repository="r",
                lakefs_client=None,
                briefcase_client=MagicMock(),
            )
            # Should not raise
            bm._emit_span_event("test.event", {"k": "v"})
