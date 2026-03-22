"""Semantic conventions for agent state management."""

# Branch operations
AGENT_BRANCH_NAME = "agent.branch.name"
AGENT_BRANCH_SOURCE = "agent.branch.source"
AGENT_BRANCH_CREATED_AT = "agent.branch.created_at"

# Merge operations
AGENT_MERGE_SOURCE = "agent.merge.source"
AGENT_MERGE_DESTINATION = "agent.merge.destination"
AGENT_MERGE_COMMIT_ID = "agent.merge.commit_id"
AGENT_MERGE_STRATEGY = "agent.merge.strategy"
AGENT_MERGE_CONFLICT = "agent.merge.conflict"  # boolean

# Diff operations
AGENT_DIFF_LEFT_REF = "agent.diff.left_ref"
AGENT_DIFF_RIGHT_REF = "agent.diff.right_ref"
AGENT_DIFF_ADDED = "agent.diff.added"      # count
AGENT_DIFF_REMOVED = "agent.diff.removed"  # count
AGENT_DIFF_CHANGED = "agent.diff.changed"  # count

# Staged validation
AGENT_STAGED_BRANCH = "agent.staged.branch"
AGENT_STAGED_TARGET = "agent.staged.target"
AGENT_STAGED_VALIDATORS_RUN = "agent.staged.validators.run"        # count
AGENT_STAGED_VALIDATORS_PASSED = "agent.staged.validators.passed"  # count
AGENT_STAGED_VALIDATORS_FAILED = "agent.staged.validators.failed"  # count
AGENT_STAGED_MODE = "agent.staged.mode"    # "cloud" or "oss"
