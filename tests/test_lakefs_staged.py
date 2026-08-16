"""Tests for StagedArtifactClient (briefcase.integrations.lakefs.staged)."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from briefcase.integrations.lakefs import (
    BranchManager,
    StagedArtifactClient,
    StagedCommitResult,
    StagedValidationError,
    ValidationResult,
)
from briefcase.integrations.lakefs.branches import BranchInfo, DiffEntry, MergeResult
from briefcase.integrations.lakefs.lineage import ArtifactLineageConfig


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_lakefs_env(monkeypatch):
    for var in (
        "LAKEFS_ENDPOINT",
        "LAKEFS_ACCESS_KEY",
        "LAKEFS_PRIVATE_KEY",
        "LAKEFS_MODE",
        "LAKEFS_STORAGE_NAMESPACE",
        "LAKEFS_BASE_URI",
        "LAKEFS_CONFIG_PATH",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def mock_branch_manager():
    bm = MagicMock(spec=BranchManager)
    bm.create_branch.return_value = BranchInfo(
        name="briefcase-staging/test",
        commit_id="mock-commit-abc",
        created_at="2026-01-01T00:00:00+00:00",
    )
    bm.delete_branch.return_value = None
    bm.merge.return_value = MergeResult(
        commit_id="merged-commit-id",
        summary={"added": 1, "removed": 0, "changed": 0},
    )
    bm.diff.return_value = [DiffEntry(path="model.pkl", type="added", size_bytes=512)]
    bm._has_lakefs = False
    return bm


@pytest.fixture
def staged_config(tmp_path):
    return ArtifactLineageConfig(
        repository="test-repo",
        branch="main",
        mode="simulate",
        local_state_dir=tmp_path / "state",
    )


@pytest.fixture
def staged_client(staged_config, mock_branch_manager):
    return StagedArtifactClient(
        config=staged_config,
        branch_manager=mock_branch_manager,
    )


@pytest.fixture
def sample_file(tmp_path):
    f = tmp_path / "model.pkl"
    f.write_bytes(b"model_data_here")
    return f


# ---------------------------------------------------------------------------
# Validator helpers
# ---------------------------------------------------------------------------


def make_passing_validator(name: str = "pass_validator"):
    def validator(branch_name, bm):
        return ValidationResult(passed=True, validator_name=name, message="ok")

    validator.__name__ = name
    return validator


def make_failing_validator(name: str = "fail_validator"):
    def validator(branch_name, bm):
        return ValidationResult(passed=False, validator_name=name, message="check failed")

    validator.__name__ = name
    return validator


def make_raising_validator(name: str = "raise_validator"):
    def validator(branch_name, bm):
        raise RuntimeError("unexpected error")

    validator.__name__ = name
    return validator


# ---------------------------------------------------------------------------
# Validation outcomes
# ---------------------------------------------------------------------------


class TestStagedArtifactClientValidation:
    def test_no_validators_merges_successfully(
        self, staged_client, mock_branch_manager, sample_file
    ):
        result = staged_client.stage_and_validate(
            files={"model.pkl": sample_file},
            message="Add model",
        )
        assert isinstance(result, StagedCommitResult)
        assert result.merged is True
        assert result.commit_id == "merged-commit-id"
        assert result.validation_results == []

    def test_both_validators_pass(
        self, staged_client, mock_branch_manager, sample_file
    ):
        staged_client.register_validator(make_passing_validator("v1"))
        staged_client.register_validator(make_passing_validator("v2"))

        result = staged_client.stage_and_validate(
            files={"model.pkl": sample_file},
            message="Add model",
        )

        assert result.merged is True
        assert len(result.validation_results) == 2
        assert all(r.passed for r in result.validation_results)

    def test_one_validator_fails_raises_staged_validation_error(
        self, staged_client, mock_branch_manager, sample_file
    ):
        staged_client.register_validator(make_passing_validator("v1"))
        staged_client.register_validator(make_failing_validator("v2"))

        with pytest.raises(StagedValidationError) as exc_info:
            staged_client.stage_and_validate(
                files={"model.pkl": sample_file},
                message="Add model",
            )

        err = exc_info.value
        assert len(err.results) == 2
        assert err.staging_branch
        passed = [r for r in err.results if r.passed]
        failed = [r for r in err.results if not r.passed]
        assert len(passed) == 1
        assert len(failed) == 1

    def test_all_validators_fail_raises(
        self, staged_client, mock_branch_manager, sample_file
    ):
        staged_client.register_validator(make_failing_validator("v1"))
        staged_client.register_validator(make_failing_validator("v2"))

        with pytest.raises(StagedValidationError) as exc_info:
            staged_client.stage_and_validate(
                files={"model.pkl": sample_file},
                message="test",
            )

        assert len(exc_info.value.results) == 2

    def test_raising_validator_treated_as_failure(
        self, staged_client, mock_branch_manager, sample_file
    ):
        staged_client.register_validator(make_raising_validator())

        with pytest.raises(StagedValidationError) as exc_info:
            staged_client.stage_and_validate(
                files={"model.pkl": sample_file},
                message="test",
            )

        failed = [r for r in exc_info.value.results if not r.passed]
        assert len(failed) == 1
        assert "unexpected error" in failed[0].message

    def test_result_contains_diff_entries(
        self, staged_client, mock_branch_manager, sample_file
    ):
        result = staged_client.stage_and_validate(
            files={"model.pkl": sample_file},
            message="test",
        )
        assert isinstance(result.diff, list)
        assert len(result.diff) == 1
        assert result.diff[0].path == "model.pkl"


# ---------------------------------------------------------------------------
# Cleanup behaviour
# ---------------------------------------------------------------------------


class TestStagedArtifactClientCleanup:
    def test_cleanup_on_success_deletes_staging_branch(
        self, staged_config, mock_branch_manager, sample_file
    ):
        client = StagedArtifactClient(
            config=staged_config,
            branch_manager=mock_branch_manager,
            cleanup_on_success=True,
        )
        client.stage_and_validate(files={"model.pkl": sample_file}, message="test")
        mock_branch_manager.delete_branch.assert_called()

    def test_no_cleanup_on_success_keeps_staging_branch(
        self, staged_config, mock_branch_manager, sample_file
    ):
        client = StagedArtifactClient(
            config=staged_config,
            branch_manager=mock_branch_manager,
            cleanup_on_success=False,
        )
        client.stage_and_validate(files={"model.pkl": sample_file}, message="test")
        mock_branch_manager.delete_branch.assert_not_called()

    def test_cleanup_on_failure_deletes_staging_branch(
        self, staged_config, mock_branch_manager, sample_file
    ):
        client = StagedArtifactClient(
            config=staged_config,
            branch_manager=mock_branch_manager,
            cleanup_on_failure=True,
        )
        client.register_validator(make_failing_validator())

        with pytest.raises(StagedValidationError):
            client.stage_and_validate(files={"model.pkl": sample_file}, message="test")

        mock_branch_manager.delete_branch.assert_called()

    def test_no_cleanup_on_failure_keeps_staging_branch(
        self, staged_config, mock_branch_manager, sample_file
    ):
        client = StagedArtifactClient(
            config=staged_config,
            branch_manager=mock_branch_manager,
            cleanup_on_failure=False,
        )
        client.register_validator(make_failing_validator())

        with pytest.raises(StagedValidationError):
            client.stage_and_validate(files={"model.pkl": sample_file}, message="test")

        mock_branch_manager.delete_branch.assert_not_called()


# ---------------------------------------------------------------------------
# Cloud vs OSS mode detection
# ---------------------------------------------------------------------------


class TestStagedArtifactClientMode:
    def test_mode_is_oss_without_live_client(
        self, staged_client
    ):
        assert staged_client.mode == "oss"

    def test_mode_cached_after_first_call(self, staged_client):
        mode1 = staged_client.mode
        mode2 = staged_client.mode
        assert mode1 == mode2

    def test_mode_cloud_when_actions_api_available(
        self, staged_config
    ):
        # Use a plain MagicMock (no spec) so that instance attributes like
        # `repository` are accessible without raising AttributeError.
        bm = MagicMock()
        bm._has_lakefs = True
        bm.repository = "test-repo"
        mock_inner_client = MagicMock()
        mock_actions_api = MagicMock()
        mock_inner_client.actions_api = mock_actions_api
        bm._lakefs_client = MagicMock()
        bm._lakefs_client._client = mock_inner_client

        client = StagedArtifactClient(
            config=staged_config,
            branch_manager=bm,
        )
        assert client.mode == "cloud"

    def test_mode_oss_when_actions_api_raises(
        self, staged_config, mock_branch_manager
    ):
        mock_branch_manager._has_lakefs = True
        mock_inner_client = MagicMock()
        mock_inner_client.actions_api.list_repository_runs.side_effect = Exception(
            "not available"
        )
        mock_branch_manager._lakefs_client = MagicMock()
        mock_branch_manager._lakefs_client._client = mock_inner_client

        client = StagedArtifactClient(
            config=staged_config,
            branch_manager=mock_branch_manager,
        )
        assert client.mode == "oss"


# ---------------------------------------------------------------------------
# from_env construction
# ---------------------------------------------------------------------------


class TestFromEnv:
    def test_from_env_returns_staged_artifact_client(self, monkeypatch):
        monkeypatch.setenv("LAKEFS_MODE", "simulate")
        client = StagedArtifactClient.from_env(repository="test-repo", branch="main")
        assert isinstance(client, StagedArtifactClient)

    def test_from_env_with_validators(self, monkeypatch):
        monkeypatch.setenv("LAKEFS_MODE", "simulate")
        validators = [make_passing_validator()]
        client = StagedArtifactClient.from_env(
            repository="test-repo",
            validators=validators,
        )
        assert len(client._validators) == 1

    def test_from_env_mode_is_oss_without_credentials(self, monkeypatch):
        # No LAKEFS_ACCESS_KEY / LAKEFS_PRIVATE_KEY (autouse fixture clears them)
        monkeypatch.setenv("LAKEFS_MODE", "simulate")
        client = StagedArtifactClient.from_env(repository="test-repo")
        assert client.mode == "oss"


# ---------------------------------------------------------------------------
# StagedCommitResult contract
# ---------------------------------------------------------------------------


class TestStagedCommitResult:
    def test_result_has_expected_fields(
        self, staged_client, mock_branch_manager, sample_file
    ):
        result = staged_client.stage_and_validate(
            files={"model.pkl": sample_file},
            message="test commit",
            staging_branch_name="briefcase-staging/custom-branch",
        )
        assert result.target_branch == "main"
        assert result.staging_branch == "briefcase-staging/custom-branch"
        assert result.commit_id == "merged-commit-id"
        assert isinstance(result.diff, list)
        assert result.mode in ("oss", "cloud")
        assert result.merged is True

    def test_custom_staging_branch_name_honoured(
        self, staged_client, mock_branch_manager, sample_file
    ):
        result = staged_client.stage_and_validate(
            files={"model.pkl": sample_file},
            message="test",
            staging_branch_name="briefcase-staging/my-run",
        )
        assert result.staging_branch == "briefcase-staging/my-run"
        # create_branch was called with the custom name
        create_call = mock_branch_manager.create_branch.call_args
        assert create_call[0][0] == "briefcase-staging/my-run"

    def test_auto_generated_staging_branch_has_prefix(
        self, staged_client, mock_branch_manager, sample_file
    ):
        result = staged_client.stage_and_validate(
            files={"model.pkl": sample_file},
            message="test",
        )
        assert result.staging_branch.startswith("briefcase-staging/")


# ---------------------------------------------------------------------------
# StagedValidationError contract
# ---------------------------------------------------------------------------


class TestStagedValidationError:
    def test_error_carries_results_and_staging_branch(
        self, staged_client, mock_branch_manager, sample_file
    ):
        staged_client.register_validator(make_failing_validator("fv"))

        with pytest.raises(StagedValidationError) as exc_info:
            staged_client.stage_and_validate(
                files={"model.pkl": sample_file},
                message="test",
            )

        err = exc_info.value
        assert hasattr(err, "results")
        assert hasattr(err, "staging_branch")
        assert any(r.validator_name == "fv" for r in err.results)


# ---------------------------------------------------------------------------
# _run_validators with non-ValidationResult return values
# ---------------------------------------------------------------------------


class TestRunValidatorsFallback:
    def test_truthy_non_result_treated_as_passed(
        self, staged_client, mock_branch_manager, sample_file
    ):
        def truthy_validator(branch, bm):
            return True  # not a ValidationResult

        truthy_validator.__name__ = "truthy_v"
        staged_client.register_validator(truthy_validator)

        result = staged_client.stage_and_validate(
            files={"model.pkl": sample_file}, message="test"
        )
        assert result.merged is True
        assert result.validation_results[0].passed is True

    def test_falsy_non_result_treated_as_failed(
        self, staged_client, mock_branch_manager, sample_file
    ):
        def falsy_validator(branch, bm):
            return None  # falsy, not a ValidationResult

        falsy_validator.__name__ = "falsy_v"
        staged_client.register_validator(falsy_validator)

        with pytest.raises(StagedValidationError) as exc_info:
            staged_client.stage_and_validate(
                files={"model.pkl": sample_file}, message="test"
            )
        assert exc_info.value.results[0].passed is False


# ---------------------------------------------------------------------------
# _try_delete_branch suppresses exceptions
# ---------------------------------------------------------------------------


class TestTryDeleteBranch:
    def test_delete_error_does_not_propagate(
        self, staged_config, mock_branch_manager, sample_file
    ):
        mock_branch_manager.delete_branch.side_effect = RuntimeError("branch locked")

        client = StagedArtifactClient(
            config=staged_config,
            branch_manager=mock_branch_manager,
            cleanup_on_success=True,
        )
        # Should not raise despite delete failing
        result = client.stage_and_validate(
            files={"model.pkl": sample_file}, message="test"
        )
        assert result.merged is True


class TestVersionFilesFailurePath:
    """Cover the except-block around the staged upload when version_files raises."""

    def test_upload_failure_propagates(
        self, staged_config, mock_branch_manager, tmp_path
    ):
        """Passing a non-existent file causes version_files to raise."""
        nonexistent = tmp_path / "missing.pkl"
        client = StagedArtifactClient(
            config=staged_config,
            branch_manager=mock_branch_manager,
        )
        with pytest.raises(Exception):
            client.stage_and_validate(
                files={"model.pkl": nonexistent},
                message="test",
            )

    def test_upload_failure_with_cleanup_on_failure_calls_delete(
        self, staged_config, mock_branch_manager, tmp_path
    ):
        nonexistent = tmp_path / "missing.pkl"
        client = StagedArtifactClient(
            config=staged_config,
            branch_manager=mock_branch_manager,
            cleanup_on_failure=True,
        )
        with pytest.raises(Exception):
            client.stage_and_validate(
                files={"model.pkl": nonexistent},
                message="test",
            )
        mock_branch_manager.delete_branch.assert_called()

    def test_upload_failure_without_cleanup_does_not_call_delete(
        self, staged_config, mock_branch_manager, tmp_path
    ):
        nonexistent = tmp_path / "missing.pkl"
        client = StagedArtifactClient(
            config=staged_config,
            branch_manager=mock_branch_manager,
            cleanup_on_failure=False,
        )
        with pytest.raises(Exception):
            client.stage_and_validate(
                files={"model.pkl": nonexistent},
                message="test",
            )
        mock_branch_manager.delete_branch.assert_not_called()


# ---------------------------------------------------------------------------
# _detect_cloud_mode null-client paths
# ---------------------------------------------------------------------------


class TestDetectCloudModeNullPaths:
    def test_oss_when_lakefs_client_has_no_inner_client(
        self, staged_config
    ):
        bm = MagicMock()
        bm._has_lakefs = True
        bm.repository = "test-repo"
        bm._lakefs_client = MagicMock()
        # No _client attribute on lakefs_client, so inner resolves to None
        del bm._lakefs_client._client

        client = StagedArtifactClient(config=staged_config, branch_manager=bm)
        assert client.mode == "oss"

    def test_oss_when_inner_client_has_no_actions_api(self, staged_config):
        bm = MagicMock()
        bm._has_lakefs = True
        bm.repository = "test-repo"
        mock_inner = MagicMock(spec=[])  # no actions_api attribute
        bm._lakefs_client = MagicMock()
        bm._lakefs_client._client = mock_inner

        client = StagedArtifactClient(config=staged_config, branch_manager=bm)
        assert client.mode == "oss"


# ---------------------------------------------------------------------------
# from_env credentials path (lakefs stubbed in sys.modules)
# ---------------------------------------------------------------------------


class TestFromEnvWithCredentials:
    def test_from_env_constructs_client_with_credentials(self, monkeypatch):
        monkeypatch.setenv("LAKEFS_MODE", "simulate")
        monkeypatch.setenv("LAKEFS_ACCESS_KEY", "test-key")
        monkeypatch.setenv("LAKEFS_PRIVATE_KEY", "test-secret")
        monkeypatch.setenv("LAKEFS_ENDPOINT", "https://lakefs.example.com/api/v1")

        stub_lakefs = MagicMock()
        stub_lakefs.Client.return_value = MagicMock()
        monkeypatch.setitem(sys.modules, "lakefs", stub_lakefs)

        client = StagedArtifactClient.from_env(repository="test-repo", branch="main")
        assert isinstance(client, StagedArtifactClient)
        stub_lakefs.Client.assert_called_once_with(
            host="https://lakefs.example.com/api/v1",
            username="test-key",
            password="test-secret",
        )

    def test_from_env_without_endpoint_skips_live_client(self, monkeypatch):
        monkeypatch.setenv("LAKEFS_MODE", "simulate")
        monkeypatch.setenv("LAKEFS_ACCESS_KEY", "test-key")
        monkeypatch.setenv("LAKEFS_PRIVATE_KEY", "test-secret")
        # LAKEFS_ENDPOINT deliberately unset

        stub_lakefs = MagicMock()
        monkeypatch.setitem(sys.modules, "lakefs", stub_lakefs)

        client = StagedArtifactClient.from_env(repository="test-repo")
        assert isinstance(client, StagedArtifactClient)
        stub_lakefs.Client.assert_not_called()
        assert client.mode == "oss"

    def test_from_env_handles_lakefs_client_failure(self, monkeypatch):
        monkeypatch.setenv("LAKEFS_MODE", "simulate")
        monkeypatch.setenv("LAKEFS_ACCESS_KEY", "key")
        monkeypatch.setenv("LAKEFS_PRIVATE_KEY", "secret")
        monkeypatch.setenv("LAKEFS_ENDPOINT", "https://lakefs.example.com/api/v1")

        stub_lakefs = MagicMock()
        stub_lakefs.Client.side_effect = ImportError("lakefs not installed")
        monkeypatch.setitem(sys.modules, "lakefs", stub_lakefs)

        client = StagedArtifactClient.from_env("test-repo")
        assert isinstance(client, StagedArtifactClient)
        assert client.mode == "oss"


# ---------------------------------------------------------------------------
# _emit_span_event in staged.py
# ---------------------------------------------------------------------------


class TestStagedEmitSpanEvent:
    def test_no_event_when_otel_unavailable(
        self, staged_client, mock_branch_manager, sample_file
    ):
        with patch("briefcase.integrations.lakefs.staged.HAS_OTEL", False):
            # Should complete without error even though OTel is "unavailable"
            result = staged_client.stage_and_validate(
                files={"model.pkl": sample_file}, message="test"
            )
        assert result.merged is True

    def test_no_event_when_span_not_recording(
        self, staged_client, mock_branch_manager, sample_file
    ):
        mock_span = MagicMock()
        mock_span.is_recording.return_value = False

        with patch("briefcase.integrations.lakefs.staged.HAS_OTEL", True), patch(
            "briefcase.integrations.lakefs.staged.trace"
        ) as mock_trace:
            mock_trace.get_current_span.return_value = mock_span
            result = staged_client.stage_and_validate(
                files={"model.pkl": sample_file}, message="test"
            )

        assert result.merged is True
        mock_span.add_event.assert_not_called()

    def test_exception_in_emit_does_not_propagate(
        self, staged_client, mock_branch_manager, sample_file
    ):
        mock_span = MagicMock()
        mock_span.is_recording.return_value = True
        mock_span.add_event.side_effect = Exception("tracer broken")

        with patch("briefcase.integrations.lakefs.staged.HAS_OTEL", True), patch(
            "briefcase.integrations.lakefs.staged.trace"
        ) as mock_trace:
            mock_trace.get_current_span.return_value = mock_span
            # Should not raise
            result = staged_client.stage_and_validate(
                files={"model.pkl": sample_file}, message="test"
            )

        assert result.merged is True
