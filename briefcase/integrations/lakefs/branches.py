"""
Branch management for lakeFS agent isolation workflows.

Provides ``BranchManager``, a high-level wrapper around the lakeFS Python SDK
for creating, merging, diffing, and cleaning up branches used by AI agents.
Runs in mock mode when no lakeFS client is supplied or the ``lakefs`` SDK is
not installed (``pip install briefcase-ai[lakefs]``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

from briefcase._logging import get_logger
from briefcase._otel import trace, HAS_OTEL
from briefcase.semantic_conventions import agent_state as agent_attrs

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data classes and enums
# ---------------------------------------------------------------------------


class MergeStrategy(str, Enum):
    """Conflict resolution strategy for lakeFS merges.

    Attributes:
        DEFAULT: Fail on conflict. The merge is rejected if there are
            conflicting changes between source and destination.
        SOURCE_WINS: The source branch wins all conflicts. Changes on the
            source override conflicting changes on the destination.
        DEST_WINS: The destination branch wins all conflicts. Conflicting
            changes from the source are discarded.
    """

    DEFAULT = "default"
    SOURCE_WINS = "source-wins"
    DEST_WINS = "dest-wins"


@dataclass
class BranchInfo:
    """Metadata about a lakeFS branch.

    Attributes:
        name: Branch name.
        commit_id: SHA of the HEAD commit.
        created_at: ISO-8601 timestamp derived from the HEAD commit creation
            date (branches do not carry their own creation timestamp in lakeFS).
    """

    name: str
    commit_id: str
    created_at: str  # ISO-8601


@dataclass
class DiffEntry:
    """A single diff entry between two lakeFS refs.

    Attributes:
        path: Object path within the repository.
        type: Change type, one of ``"added"``, ``"removed"``, or
            ``"changed"``.
        size_bytes: Object size in bytes, if available.
    """

    path: str
    type: str  # "added", "removed", "changed"
    size_bytes: Optional[int] = None


@dataclass
class MergeResult:
    """Result of a successful lakeFS merge.

    Attributes:
        commit_id: SHA of the merge commit on the destination branch.
        summary: Change counts keyed by ``"added"``, ``"removed"``, and
            ``"changed"``.
    """

    commit_id: str
    summary: dict  # {"added": int, "removed": int, "changed": int}


# ---------------------------------------------------------------------------
# BranchManager
# ---------------------------------------------------------------------------


class BranchManager:
    """Manages lakeFS branches for agent isolation workflows.

    Wraps lakeFS branch/merge/diff operations via the ``lakefs`` Python SDK.
    Runs in mock mode (returning fake but structurally valid data, with
    ``mock-`` prefixed commit ids) when ``lakefs_client`` is ``None`` or the
    SDK is not installed, so no mock value is ever mistaken for real
    provenance.

    Args:
        repository: lakeFS repository name.
        lakefs_client: Authenticated ``lakefs.Client`` instance (or ``None``
            for mock mode).
        default_source_branch: Branch to use as the source when
            :meth:`create_branch` is called without an explicit ``source``.
        briefcase_client: Optional Briefcase client used to gate OTel span
            event emission.
        default_strategy: Default :class:`MergeStrategy` used when
            :meth:`merge` is called without an explicit ``strategy``.

    Example::

        from briefcase.integrations.lakefs import BranchManager

        bm = BranchManager(repository="my-repo", lakefs_client=client)
        info = bm.create_branch("agent/run-42")
        # ... agent operates on the branch ...
        bm.merge("agent/run-42", "main")
        bm.delete_branch("agent/run-42")
    """

    def __init__(
        self,
        repository: str,
        lakefs_client: Any,
        default_source_branch: str = "main",
        briefcase_client: Any = None,
        default_strategy: MergeStrategy = MergeStrategy.DEFAULT,
    ) -> None:
        self.repository = repository
        self._lakefs_client = lakefs_client
        self.default_source_branch = default_source_branch
        self.briefcase_client = briefcase_client
        self.default_strategy = default_strategy

        self._lakefs: Any = None
        self._has_lakefs = False
        try:
            import lakefs

            self._lakefs = lakefs
            self._has_lakefs = lakefs_client is not None
        except ImportError:
            logger.warning(
                "lakefs SDK not available (install with "
                "'pip install briefcase-ai[lakefs]'); "
                "BranchManager running in mock mode."
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_branch(self, name: str, source: str | None = None) -> BranchInfo:
        """Create a new lakeFS branch.

        Args:
            name: Name of the new branch.
            source: Source branch or commit ref to create from. Defaults to
                ``self.default_source_branch``.

        Returns:
            :class:`BranchInfo` describing the newly created branch.

        Raises:
            RuntimeError: If branch creation fails when the SDK is available.
        """
        source = source or self.default_source_branch

        if not self._has_lakefs:
            info = BranchInfo(
                name=name,
                commit_id=f"mock-{name[:8].replace('/', '-')}-commit",
                created_at=datetime.now(tz=timezone.utc).isoformat(),
            )
            self._emit_span_event(
                "lakefs.branch_created",
                {
                    agent_attrs.AGENT_BRANCH_NAME: name,
                    agent_attrs.AGENT_BRANCH_SOURCE: source,
                    agent_attrs.AGENT_BRANCH_CREATED_AT: info.created_at,
                },
            )
            return info

        try:
            repo = self._repo()
            branch = repo.branch(name)
            branch.create(source_reference=source)
            commit = branch.get_commit()
            commit_id = getattr(commit, "id", str(commit))
            created_at = self._commit_timestamp(commit)
            info = BranchInfo(name=name, commit_id=commit_id, created_at=created_at)
            self._emit_span_event(
                "lakefs.branch_created",
                {
                    agent_attrs.AGENT_BRANCH_NAME: name,
                    agent_attrs.AGENT_BRANCH_SOURCE: source,
                    agent_attrs.AGENT_BRANCH_CREATED_AT: created_at,
                },
            )
            return info
        except Exception as exc:
            logger.error("Failed to create branch %s: %s", name, exc)
            raise RuntimeError(f"Failed to create branch '{name}'") from exc

    def delete_branch(self, name: str) -> None:
        """Delete a lakeFS branch.

        Args:
            name: Name of the branch to delete.

        Raises:
            RuntimeError: If deletion fails when the SDK is available.
        """
        if not self._has_lakefs:
            logger.info("Mock mode: would delete branch %s", name)
            return

        try:
            self._repo().branch(name).delete()
        except Exception as exc:
            logger.error("Failed to delete branch %s: %s", name, exc)
            raise RuntimeError(f"Failed to delete branch '{name}'") from exc

    def merge(
        self,
        source: str,
        destination: str,
        message: str | None = None,
        strategy: MergeStrategy | None = None,
    ) -> MergeResult:
        """Merge *source* branch into *destination*.

        Args:
            source: Source branch name.
            destination: Destination branch name.
            message: Optional merge commit message.
            strategy: Conflict resolution strategy. ``None`` falls back to
                ``self.default_strategy``.

        Returns:
            :class:`MergeResult` with ``commit_id`` and ``summary`` dict.

        Raises:
            RuntimeError: If the merge fails or there is a conflict when
                using :attr:`MergeStrategy.DEFAULT`.
        """
        effective_strategy = (
            strategy if strategy is not None else self.default_strategy
        )

        if not self._has_lakefs:
            result = MergeResult(
                commit_id=f"mock-merge-{source[:6]}-into-{destination[:6]}",
                summary={"added": 0, "removed": 0, "changed": 0},
            )
            self._emit_span_event(
                "lakefs.branch_merged",
                {
                    agent_attrs.AGENT_MERGE_SOURCE: source,
                    agent_attrs.AGENT_MERGE_DESTINATION: destination,
                    agent_attrs.AGENT_MERGE_COMMIT_ID: result.commit_id,
                    agent_attrs.AGENT_MERGE_STRATEGY: effective_strategy.value,
                    agent_attrs.AGENT_MERGE_CONFLICT: False,
                },
            )
            return result

        try:
            repo = self._repo()
            source_branch = repo.branch(source)
            kwargs: dict[str, Any] = {}
            if message:
                kwargs["message"] = message
            if effective_strategy != MergeStrategy.DEFAULT:
                kwargs["strategy"] = effective_strategy.value

            raw = source_branch.merge_into(destination, **kwargs)
            commit_id = getattr(raw, "id", str(raw))
            summary = getattr(raw, "summary", {}) or {}
            if not isinstance(summary, dict):
                summary = {}
            result = MergeResult(commit_id=commit_id, summary=summary)
            self._emit_span_event(
                "lakefs.branch_merged",
                {
                    agent_attrs.AGENT_MERGE_SOURCE: source,
                    agent_attrs.AGENT_MERGE_DESTINATION: destination,
                    agent_attrs.AGENT_MERGE_COMMIT_ID: commit_id,
                    agent_attrs.AGENT_MERGE_STRATEGY: effective_strategy.value,
                    agent_attrs.AGENT_MERGE_CONFLICT: False,
                },
            )
            return result
        except Exception as exc:
            error_lower = str(exc).lower()
            is_conflict = "conflict" in error_lower
            self._emit_span_event(
                "lakefs.branch_merged",
                {
                    agent_attrs.AGENT_MERGE_SOURCE: source,
                    agent_attrs.AGENT_MERGE_DESTINATION: destination,
                    agent_attrs.AGENT_MERGE_STRATEGY: effective_strategy.value,
                    agent_attrs.AGENT_MERGE_CONFLICT: is_conflict,
                },
            )
            logger.error("Failed to merge %s into %s: %s", source, destination, exc)
            raise RuntimeError(
                f"Failed to merge '{source}' into '{destination}'"
            ) from exc

    def diff(self, left_ref: str, right_ref: str) -> list[DiffEntry]:
        """Compute the diff between two lakeFS refs.

        Args:
            left_ref: Left reference (branch name or commit SHA).
            right_ref: Right reference (branch name or commit SHA).

        Returns:
            List of :class:`DiffEntry` objects. Returns an empty list on
            error or in mock mode.
        """
        if not self._has_lakefs:
            self._emit_span_event(
                "lakefs.diff_computed",
                {
                    agent_attrs.AGENT_DIFF_LEFT_REF: left_ref,
                    agent_attrs.AGENT_DIFF_RIGHT_REF: right_ref,
                    agent_attrs.AGENT_DIFF_ADDED: 0,
                    agent_attrs.AGENT_DIFF_REMOVED: 0,
                    agent_attrs.AGENT_DIFF_CHANGED: 0,
                },
            )
            return []

        try:
            repo = self._repo()
            left = repo.ref(left_ref)
            entries: list[DiffEntry] = []
            for item in left.diff(other=right_ref):
                path = getattr(item, "path", "")
                raw_type = str(getattr(item, "type", "changed")).lower().replace("_", " ")
                if "add" in raw_type:
                    type_str = "added"
                elif "remov" in raw_type or "delet" in raw_type:
                    type_str = "removed"
                else:
                    type_str = "changed"
                size = getattr(item, "size_bytes", None)
                entries.append(DiffEntry(path=path, type=type_str, size_bytes=size))

            added = sum(1 for e in entries if e.type == "added")
            removed = sum(1 for e in entries if e.type == "removed")
            changed = sum(1 for e in entries if e.type == "changed")
            self._emit_span_event(
                "lakefs.diff_computed",
                {
                    agent_attrs.AGENT_DIFF_LEFT_REF: left_ref,
                    agent_attrs.AGENT_DIFF_RIGHT_REF: right_ref,
                    agent_attrs.AGENT_DIFF_ADDED: added,
                    agent_attrs.AGENT_DIFF_REMOVED: removed,
                    agent_attrs.AGENT_DIFF_CHANGED: changed,
                },
            )
            return entries
        except Exception as exc:
            logger.error("Failed to diff %s..%s: %s", left_ref, right_ref, exc)
            return []

    def branch_exists(self, name: str) -> bool:
        """Check whether a branch exists in the repository.

        Args:
            name: Branch name to check.

        Returns:
            ``True`` if the branch exists, ``False`` otherwise (including in
            mock mode).
        """
        if not self._has_lakefs:
            return False

        try:
            self._repo().branch(name).get_commit()
            return True
        except Exception:
            return False

    def list_branches(self, prefix: str = "") -> list[BranchInfo]:
        """List branches in the repository.

        Args:
            prefix: Optional prefix filter applied to branch names.

        Returns:
            List of :class:`BranchInfo` objects. Returns an empty list in
            mock mode or on error.
        """
        if not self._has_lakefs:
            return []

        try:
            results: list[BranchInfo] = []
            for branch in self._repo().branches(prefix=prefix):
                name = getattr(branch, "id", str(branch))
                try:
                    commit = (
                        branch.get_commit()
                        if hasattr(branch, "get_commit")
                        else None
                    )
                    commit_id = (
                        getattr(commit, "id", "unknown") if commit else "unknown"
                    )
                    created_at = (
                        self._commit_timestamp(commit)
                        if commit
                        else datetime.now(tz=timezone.utc).isoformat()
                    )
                except Exception:
                    commit_id = "unknown"
                    created_at = datetime.now(tz=timezone.utc).isoformat()
                results.append(
                    BranchInfo(name=name, commit_id=commit_id, created_at=created_at)
                )
            return results
        except Exception as exc:
            logger.error("Failed to list branches: %s", exc)
            return []

    def cleanup_stale_branches(self, max_age_hours: int = 24) -> list[str]:
        """Delete staging branches older than *max_age_hours*.

        Lists all branches whose names start with ``briefcase-staging/`` and
        deletes those whose HEAD-commit timestamp is older than the age
        threshold. Uses :meth:`list_branches` and :meth:`delete_branch`
        internally, so it works in both live and mock modes (though in mock
        mode :meth:`list_branches` returns an empty list).

        Args:
            max_age_hours: Age threshold in hours. Branches older than this
                are deleted. Defaults to 24.

        Returns:
            List of branch names that were deleted.
        """
        deleted: list[str] = []
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=max_age_hours)

        for info in self.list_branches(prefix="briefcase-staging/"):
            try:
                created = datetime.fromisoformat(info.created_at)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created < cutoff:
                    self.delete_branch(info.name)
                    deleted.append(info.name)
            except Exception as exc:
                logger.warning(
                    "Could not process branch %s for cleanup: %s", info.name, exc
                )

        return deleted

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _repo(self) -> Any:
        """Return a ``lakefs.Repository`` handle.

        Raises:
            RuntimeError: If the lakeFS SDK or client is not initialized.
        """
        if not self._has_lakefs or not self._lakefs:
            raise RuntimeError("lakeFS client not initialized")
        return self._lakefs.Repository(self.repository, client=self._lakefs_client)

    @staticmethod
    def _commit_timestamp(commit: Any) -> str:
        """Extract an ISO-8601 timestamp from a lakeFS commit object."""
        for attr in ("creation_date", "created_at", "timestamp"):
            val = getattr(commit, attr, None)
            if val is None:
                continue
            if isinstance(val, (int, float)):
                return datetime.fromtimestamp(
                    float(val) / 1000.0, tz=timezone.utc
                ).isoformat()
            if isinstance(val, datetime):
                if val.tzinfo is None:
                    val = val.replace(tzinfo=timezone.utc)
                return val.isoformat()
            return str(val)
        return datetime.now(tz=timezone.utc).isoformat()

    def _emit_span_event(self, name: str, attributes: dict[str, Any]) -> None:
        """Emit an OTel span event when a briefcase client is present."""
        if not self.briefcase_client or not HAS_OTEL:
            return
        try:
            span = trace.get_current_span()
            if span and span.is_recording():
                span.add_event(name, attributes=attributes)
        except Exception:
            pass
