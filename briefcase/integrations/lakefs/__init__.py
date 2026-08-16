"""
lakeFS integration for automatic commit SHA capture.

This module provides automatic versioning for AI agents that access
knowledge bases stored in lakeFS.

Usage:
    # Context manager (recommended)
    from briefcase import BriefcaseClient
    from briefcase.integrations.lakefs import versioned_context

    client = BriefcaseClient(config)

    with versioned_context(client, "acme-workspace", "main") as versioned:
        policy = versioned.read_object("policies/policy.pdf")
        # Automatically tracked with commit SHA

    # Decorator
    from briefcase.integrations.lakefs import versioned

    @versioned(repository="acme-workspace")
    def evaluate_claim(claim_data, versioned_client):
        policy = versioned_client.read_object("policies/policy.pdf")
        return evaluate(claim_data, policy)

    # Direct client
    from briefcase.integrations.lakefs import VersionedClient

    versioned_client = VersionedClient(
        repository="acme-workspace",
        branch="main",
        briefcase_client=client
    )
    policy = versioned_client.read_object("policies/policy.pdf")
"""

try:
    from briefcase.integrations.lakefs.client import (
        VersionedClient,
        InstrumentedLakeFSClient,
    )
    from briefcase.integrations.lakefs.lineage import (
        ArtifactLineageClient,
        ArtifactLineageConfig,
        ArtifactCommitInfo,
        ArtifactLineageError,
    )
    from briefcase.integrations.lakefs.context import (
        versioned_context,
        VersionedContextManager,
        lakefs_context,
        LakeFSContextManager,
    )
    from briefcase.integrations.lakefs.decorators import versioned, lakefs_versioned
    from briefcase.integrations.lakefs.branches import (
        BranchManager,
        BranchInfo,
        DiffEntry,
        MergeResult,
        MergeStrategy,
    )
    from briefcase.integrations.lakefs.staged import (
        StagedArtifactClient,
        StagedCommitResult,
        StagedValidationError,
        ValidationResult,
    )
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "briefcase.integrations.lakefs requires the 'lakefs' extra.\n"
        "Install it with: pip install briefcase-ai[lakefs]"
    ) from exc

__all__ = [
    "VersionedClient",
    "ArtifactLineageClient",
    "ArtifactLineageConfig",
    "ArtifactCommitInfo",
    "ArtifactLineageError",
    "versioned_context",
    "VersionedContextManager",
    "versioned",
    "BranchManager",
    "BranchInfo",
    "DiffEntry",
    "MergeResult",
    "MergeStrategy",
    "StagedArtifactClient",
    "StagedCommitResult",
    "StagedValidationError",
    "ValidationResult",
    # Backwards compatibility
    "InstrumentedLakeFSClient",
    "lakefs_context",
    "LakeFSContextManager",
    "lakefs_versioned",
]
