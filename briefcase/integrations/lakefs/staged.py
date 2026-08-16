"""
Staged artifact client for branch-based validation and atomic merge.

Implements a trust-but-verify pattern:

1. Create an ephemeral staging branch from the target branch.
2. Upload artifacts to the staging branch.
3. Run registered validators against the staging branch.
4. If all pass: merge staging into target (and optionally delete the
   staging branch).
5. If any fail: raise :class:`StagedValidationError`, leaving the staging
   branch intact for inspection (configurable via ``cleanup_on_failure``).

Supports both lakeFS Cloud (server-side action hooks) and lakeFS OSS
(client-side validation). Cloud vs OSS is auto-detected at runtime.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from briefcase._logging import get_logger
from briefcase._otel import trace, HAS_OTEL
from briefcase.integrations.lakefs.branches import BranchManager, DiffEntry, MergeStrategy
from briefcase.integrations.lakefs.lineage import (
    ArtifactLineageClient,
    ArtifactLineageConfig,
    ArtifactLineageError,
)
from briefcase.semantic_conventions import agent_state as agent_attrs

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data classes and exceptions
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Result from a single validator function.

    Attributes:
        passed: Whether the validator considered the staging branch valid.
        validator_name: Human-readable name of the validator.
        message: Descriptive message (error reason when ``passed=False``).
        details: Optional structured details dict.
    """

    passed: bool
    validator_name: str
    message: str
    details: dict = field(default_factory=dict)


@dataclass
class StagedCommitResult:
    """Outcome of a successful :meth:`StagedArtifactClient.stage_and_validate` call.

    Attributes:
        commit_id: SHA of the merge commit on the target branch.
        staging_branch: Ephemeral staging branch that was used.
        target_branch: Destination branch that received the merge.
        validation_results: Results from all registered validators.
        diff: Object-level diff between target and staging branches.
        mode: ``"cloud"`` if lakeFS Cloud actions were used, else ``"oss"``.
        merged: Always ``True`` (the field exists to make the schema explicit).
    """

    commit_id: str
    staging_branch: str
    target_branch: str
    validation_results: list[ValidationResult]
    diff: list[DiffEntry]
    mode: str  # "cloud" or "oss"
    merged: bool


class StagedValidationError(ArtifactLineageError):
    """Raised when one or more staging validators fail.

    Attributes:
        results: Full list of :class:`ValidationResult` objects (passed and
            failed).
        staging_branch: Name of the staging branch that remains for
            inspection.
    """

    def __init__(
        self,
        message: str,
        results: list[ValidationResult],
        staging_branch: str,
    ) -> None:
        super().__init__(message)
        self.results = results
        self.staging_branch = staging_branch


# ---------------------------------------------------------------------------
# StagedArtifactClient
# ---------------------------------------------------------------------------


class StagedArtifactClient:
    """Artifact lineage with branch-based staging and pre-merge validation.

    Workflow
    --------
    1. Create ephemeral staging branch from target branch.
    2. Upload artifacts to staging branch.
    3. Run registered validators against staging branch.
    4. If all pass: merge staging into target, delete staging branch.
    5. If any fail: raise :class:`StagedValidationError`, leave staging branch
       for inspection.

    Supports both lakeFS Cloud (server-side hooks) and lakeFS OSS
    (client-side validation only). Auto-detects which mode is available.

    Args:
        config: :class:`~briefcase.integrations.lakefs.lineage.ArtifactLineageConfig`
            describing the repository and target branch.
        branch_manager: :class:`~briefcase.integrations.lakefs.branches.BranchManager`
            instance for branch/merge/diff operations.
        validators: Initial list of validator callables. Each callable
            receives ``(staging_branch_name: str, branch_manager: BranchManager)``
            and must return a :class:`ValidationResult`.
        merge_strategy: :class:`~briefcase.integrations.lakefs.branches.MergeStrategy`
            used when merging the staging branch.
        cleanup_on_success: Delete the staging branch after a successful
            merge. Defaults to ``True``.
        cleanup_on_failure: Delete the staging branch when validation fails.
            Defaults to ``False`` (keep for inspection).

    Example::

        from briefcase.integrations.lakefs import StagedArtifactClient, BranchManager

        def size_validator(branch, bm):
            entries = bm.diff("main", branch)
            large = [e for e in entries if (e.size_bytes or 0) > 100_000_000]
            if large:
                return ValidationResult(False, "size_validator", f"{len(large)} objects too large")
            return ValidationResult(True, "size_validator", "ok")

        client = StagedArtifactClient.from_env("my-repo", validators=[size_validator])
        result = client.stage_and_validate({"model.pkl": Path("model.pkl")}, "Train run #5")
        print(result.commit_id)
    """

    def __init__(
        self,
        config: ArtifactLineageConfig,
        branch_manager: BranchManager,
        validators: Optional[list[Callable]] = None,
        merge_strategy: MergeStrategy = MergeStrategy.DEFAULT,
        cleanup_on_success: bool = True,
        cleanup_on_failure: bool = False,
    ) -> None:
        self._config = config
        self._branch_manager = branch_manager
        self._validators: list[Callable] = list(validators or [])
        self._merge_strategy = merge_strategy
        self._cleanup_on_success = cleanup_on_success
        self._cleanup_on_failure = cleanup_on_failure

        # Cloud detection is cached after first call.
        self._cloud_mode: Optional[bool] = None

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_env(
        cls,
        repository: str,
        branch: str = "main",
        validators: Optional[list[Callable]] = None,
    ) -> StagedArtifactClient:
        """Build a :class:`StagedArtifactClient` from environment variables.

        Uses the same environment variables as
        :meth:`~briefcase.integrations.lakefs.lineage.ArtifactLineageClient.from_env`
        plus the ``LAKEFS_ENDPOINT``, ``LAKEFS_ACCESS_KEY``, and
        ``LAKEFS_PRIVATE_KEY`` credential variables consumed by
        :class:`~briefcase.integrations.lakefs.client.VersionedClient`.
        When any credential variable is missing, or the ``lakefs`` SDK is
        not installed, the underlying :class:`BranchManager` runs in mock
        mode.

        Args:
            repository: lakeFS repository name.
            branch: Target branch. Defaults to ``"main"``.
            validators: Optional list of validator callables.

        Returns:
            A configured :class:`StagedArtifactClient` instance.
        """
        config = ArtifactLineageConfig(
            repository=repository,
            branch=branch,
            mode=os.getenv("LAKEFS_MODE", "auto"),
            storage_namespace=os.getenv("LAKEFS_STORAGE_NAMESPACE"),
            base_uri=os.getenv("LAKEFS_BASE_URI"),
            config_path=os.getenv("LAKEFS_CONFIG_PATH"),
        )

        lakefs_client = None
        try:
            import lakefs

            endpoint = os.getenv("LAKEFS_ENDPOINT")
            access_key = os.getenv("LAKEFS_ACCESS_KEY")
            secret_key = os.getenv("LAKEFS_PRIVATE_KEY")
            if endpoint and access_key and secret_key:
                lakefs_client = lakefs.Client(
                    host=endpoint,
                    username=access_key,
                    password=secret_key,
                )
        except Exception as exc:
            logger.debug("Could not initialise live lakeFS client: %s", exc)

        branch_manager = BranchManager(
            repository=repository,
            lakefs_client=lakefs_client,
            default_source_branch=branch,
        )
        return cls(config=config, branch_manager=branch_manager, validators=validators)

    # ------------------------------------------------------------------
    # Validator registration
    # ------------------------------------------------------------------

    def register_validator(
        self, validator: Callable[[str, BranchManager], ValidationResult]
    ) -> None:
        """Register a validator to run against the staging branch.

        The *validator* is called with ``(staging_branch_name, branch_manager)``
        before the merge. It must return a :class:`ValidationResult`.

        Args:
            validator: Callable that accepts the staging branch name (``str``)
                and a :class:`BranchManager` and returns a
                :class:`ValidationResult`.
        """
        self._validators.append(validator)

    # ------------------------------------------------------------------
    # Core workflow
    # ------------------------------------------------------------------

    def stage_and_validate(
        self,
        files: dict[str, str | Path],
        message: str,
        metadata: Optional[dict[str, str]] = None,
        staging_branch_name: Optional[str] = None,
    ) -> StagedCommitResult:
        """Run the full stage, validate, and merge workflow.

        Args:
            files: Mapping of ``{lakeFS object path: local file path}`` to
                upload to the staging branch.
            message: Commit message for the staged upload.
            metadata: Optional key-value metadata attached to the staging
                commit.
            staging_branch_name: Name for the ephemeral staging branch.
                Auto-generated with a ``briefcase-staging/`` prefix when
                ``None``.

        Returns:
            :class:`StagedCommitResult` describing the outcome.

        Raises:
            StagedValidationError: If one or more validators fail. The
                staging branch is preserved for inspection unless
                ``cleanup_on_failure=True``.
            ArtifactLineageError: If the file upload itself fails.
        """
        target_branch = self._config.branch

        if staging_branch_name is None:
            ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S")
            staging_branch_name = f"briefcase-staging/{ts}"

        # 1. Create staging branch.
        self._branch_manager.create_branch(staging_branch_name, source=target_branch)

        # 2. Upload files to the staging branch.
        staging_config = ArtifactLineageConfig(
            repository=self._config.repository,
            branch=staging_branch_name,
            mode=self._config.mode,
            storage_namespace=self._config.storage_namespace,
            base_uri=self._config.base_uri,
            config_path=self._config.config_path,
            local_state_dir=self._config.local_state_dir,
        )
        staging_client = ArtifactLineageClient(staging_config)

        try:
            staging_client.version_files(files, message, metadata)
        except Exception:
            if self._cleanup_on_failure:
                self._try_delete_branch(staging_branch_name)
            raise

        # 3. Compute diff between target and staging.
        diff_entries = self._branch_manager.diff(target_branch, staging_branch_name)

        # 4. Run client-side validators.
        validation_results = self._run_validators(staging_branch_name)

        all_passed = all(r.passed for r in validation_results)
        n_passed = sum(1 for r in validation_results if r.passed)
        n_failed = len(validation_results) - n_passed

        self._emit_span_event(
            "lakefs.staged_validation",
            {
                agent_attrs.AGENT_STAGED_BRANCH: staging_branch_name,
                agent_attrs.AGENT_STAGED_TARGET: target_branch,
                agent_attrs.AGENT_STAGED_VALIDATORS_RUN: len(validation_results),
                agent_attrs.AGENT_STAGED_VALIDATORS_PASSED: n_passed,
                agent_attrs.AGENT_STAGED_VALIDATORS_FAILED: n_failed,
                agent_attrs.AGENT_STAGED_MODE: self.mode,
            },
        )

        if not all_passed:
            if self._cleanup_on_failure:
                self._try_delete_branch(staging_branch_name)
            raise StagedValidationError(
                f"{n_failed} validator(s) failed on staging branch "
                f"'{staging_branch_name}'",
                results=validation_results,
                staging_branch=staging_branch_name,
            )

        # 5. Merge staging into target.
        merge_result = self._branch_manager.merge(
            source=staging_branch_name,
            destination=target_branch,
            message=f"merge: {message}",
            strategy=self._merge_strategy,
        )

        # 6. Clean up staging branch on success if requested.
        if self._cleanup_on_success:
            self._try_delete_branch(staging_branch_name)

        return StagedCommitResult(
            commit_id=merge_result.commit_id,
            staging_branch=staging_branch_name,
            target_branch=target_branch,
            validation_results=validation_results,
            diff=diff_entries,
            mode=self.mode,
            merged=True,
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def mode(self) -> str:
        """Return ``"cloud"`` if lakeFS Cloud is detected, else ``"oss"``.

        The result is cached after the first call.
        """
        if self._cloud_mode is None:
            self._cloud_mode = self._detect_cloud_mode()
        return "cloud" if self._cloud_mode else "oss"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_validators(self, staging_branch_name: str) -> list[ValidationResult]:
        """Run all registered validators and collect results."""
        results: list[ValidationResult] = []
        for validator in self._validators:
            name = getattr(validator, "__name__", repr(validator))
            try:
                raw = validator(staging_branch_name, self._branch_manager)
                if isinstance(raw, ValidationResult):
                    results.append(raw)
                else:
                    results.append(
                        ValidationResult(
                            passed=bool(raw),
                            validator_name=name,
                            message="",
                        )
                    )
            except Exception as exc:
                results.append(
                    ValidationResult(
                        passed=False,
                        validator_name=name,
                        message=str(exc),
                    )
                )
        return results

    def _try_delete_branch(self, name: str) -> None:
        """Best-effort branch deletion; logs but does not re-raise on error."""
        try:
            self._branch_manager.delete_branch(name)
        except Exception as exc:
            logger.warning("Failed to delete staging branch %s: %s", name, exc)

    def _detect_cloud_mode(self) -> bool:
        """Probe the lakeFS instance to determine whether Cloud features are available.

        Attempts a lightweight call to the actions API. Returns ``False`` in
        mock mode or if the probe fails (indicating OSS or insufficient
        permissions).
        """
        if not self._branch_manager._has_lakefs:
            return False

        try:
            client = self._branch_manager._lakefs_client
            # The lakefs Python SDK wraps a generated OpenAPI client.
            # Check for the internal _client attribute that exposes raw API objects.
            inner = getattr(client, "_client", None)
            if inner is None:
                return False
            actions_api = getattr(inner, "actions_api", None)
            if actions_api is None:
                return False
            actions_api.list_repository_runs(
                repository=self._branch_manager.repository,
                max_amount=1,
            )
            return True
        except Exception:
            return False

    def _emit_span_event(self, name: str, attributes: dict[str, Any]) -> None:
        """Emit an OTel span event when the OTel SDK is present."""
        if not HAS_OTEL:
            return
        try:
            span = trace.get_current_span()
            if span and span.is_recording():
                span.add_event(name, attributes=attributes)
        except Exception:
            pass
