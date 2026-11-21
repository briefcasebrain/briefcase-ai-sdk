"""Tests for VersionedEmbeddingPipeline's lakeFS client call conventions."""

import pytest

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
        lakefs_client=lakefs, repository="repo", branch="main", mock=True
    )
    batch = pipeline.create_embedding_batch([Document(id="d1", content="hello")])
    assert batch.source_commit == "c0ffee0000000000000000000000000000000000"


def test_manifest_upload_matches_versioned_client_signature():
    """create_manifest calls upload_object(path, data) as defined by
    VersionedClient, so manifests reach a real client."""
    lakefs = VersionedClientDouble()
    pipeline = VersionedEmbeddingPipeline(
        lakefs_client=lakefs, repository="repo", branch="main", mock=True
    )
    batch = pipeline.create_embedding_batch([Document(id="d1", content="hello")])
    manifest = pipeline.create_manifest("idx", [batch])
    path = f"manifests/idx/{manifest.manifest_id}.json"
    assert path in pipeline.lakefs.uploads
    assert isinstance(lakefs.uploads[path], bytes)


class _Model:
    name = "real-model"
    version = "2.1"

    def __init__(self, fail=False, short=False):
        self.fail = fail
        self.short = short

    def embed(self, texts):
        if self.fail:
            raise RuntimeError("provider 503")
        vectors = [[float(len(t)), 1.0] for t in texts]
        return vectors[:-1] if self.short else vectors


def test_model_failure_raises_and_stores_no_fake_vectors():
    """A failing model must not yield zero vectors labelled with its name."""
    pipeline = VersionedEmbeddingPipeline(embedding_model=_Model(fail=True))
    with pytest.raises(RuntimeError, match="provider 503"):
        pipeline.create_embedding_batch([Document(id="d1", content="hello")], batch_id="b1")
    assert pipeline._batches == {}


def test_no_model_requires_explicit_mock():
    with pytest.raises(ValueError, match="mock=True"):
        VersionedEmbeddingPipeline().create_embedding_batch(
            [Document(id="d1", content="hello")]
        )
    batch = VersionedEmbeddingPipeline(mock=True).create_embedding_batch(
        [Document(id="d1", content="hello")]
    )
    assert batch.model == "mock-model"


def test_model_without_embed_or_with_short_output_raises():
    with pytest.raises(TypeError, match="embed"):
        VersionedEmbeddingPipeline(embedding_model=object()).create_embedding_batch(
            [Document(id="d1", content="hello")]
        )
    with pytest.raises(ValueError, match="1 vectors for 2 documents"):
        VersionedEmbeddingPipeline(embedding_model=_Model(short=True)).create_embedding_batch(
            [Document(id="d1", content="a"), Document(id="d2", content="b")]
        )


def test_real_model_batch_is_labelled_and_timezone_aware():
    pipeline = VersionedEmbeddingPipeline(embedding_model=_Model())
    batch = pipeline.create_embedding_batch([Document(id="d1", content="hello")])
    assert batch.embeddings == [[5.0, 1.0]]
    assert (batch.model, batch.model_version) == ("real-model", "2.1")
    assert batch.created_at.tzinfo is not None
    assert batch.created_at.utcoffset().total_seconds() == 0
