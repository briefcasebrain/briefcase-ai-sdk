"""Tests for VersionedEmbeddingPipeline's lakeFS client call conventions."""

from briefcase.rag.embedding_pipeline import (
    Document,
    VersionedEmbeddingPipeline,
)


class VersionedClientDouble:
    """Fake with VersionedClient's exact method signatures."""

    def __init__(self):
        self.uploads = {}

    def upload_object(self, path, data, content_type="application/octet-stream"):
        self.uploads[path] = data

    def get_commit(self):
        return "c0ffee0000000000000000000000000000000000"


def test_batch_source_commit_uses_no_arg_get_commit():
    lakefs = VersionedClientDouble()
    pipeline = VersionedEmbeddingPipeline(
        lakefs_client=lakefs, repository="repo", branch="main"
    )
    batch = pipeline.create_embedding_batch([Document(id="d1", content="hello")])
    assert batch.source_commit == "c0ffee0000000000000000000000000000000000"


def test_manifest_upload_matches_versioned_client_signature():
    """create_manifest calls upload_object(path, data) as defined by
    VersionedClient, so manifests reach a real client."""
    lakefs = VersionedClientDouble()
    pipeline = VersionedEmbeddingPipeline(
        lakefs_client=lakefs, repository="repo", branch="main"
    )
    batch = pipeline.create_embedding_batch([Document(id="d1", content="hello")])
    manifest = pipeline.create_manifest("idx", [batch])
    path = f"manifests/idx/{manifest.manifest_id}.json"
    assert path in pipeline.lakefs.uploads
    assert isinstance(lakefs.uploads[path], bytes)
