"""
Semantic conventions for lakeFS versioning attributes.

These attributes should be used consistently across all lakeFS integrations
to ensure telemetry data is queryable and comparable.
"""

# Commit-level attributes
LAKEFS_COMMIT_SHA = "lakefs.commit.sha"
LAKEFS_COMMIT_BRANCH = "lakefs.commit.branch"
LAKEFS_COMMIT_TIMESTAMP = "lakefs.commit.timestamp"
LAKEFS_COMMIT_MESSAGE = "lakefs.commit.message"
LAKEFS_COMMIT_AUTHOR = "lakefs.commit.author"

# Repository/workstream attributes
LAKEFS_REPOSITORY = "lakefs.repository"
LAKEFS_BRANCH = "lakefs.branch"
LAKEFS_ENGAGEMENT_ID = "lakefs.engagement_id"
LAKEFS_WORKSTREAM_ID = "lakefs.workstream_id"

# File-level attributes
LAKEFS_FILE_PATH = "lakefs.file.path"
LAKEFS_FILE_SIZE = "lakefs.file.size"
LAKEFS_FILE_MODIFIED = "lakefs.file.modified"
LAKEFS_FILE_HASH = "lakefs.file.hash"
LAKEFS_FILE_CONTENT_TYPE = "lakefs.file.content_type"

# Artifact-level attributes (for specific files accessed)
# Pattern: lakefs.artifact.{file_path} = commit_sha
# Example: lakefs.artifact.policies/aetna.pdf = "abc123..."
LAKEFS_ARTIFACT_PREFIX = "lakefs.artifact."

# Operation attributes
LAKEFS_OPERATION = "lakefs.operation"  # read, write, list, etc.
LAKEFS_ACCESS_TIME = "lakefs.access.timestamp"
