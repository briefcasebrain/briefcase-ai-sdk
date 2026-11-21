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


# Ids

def test_default_batch_ids_do_not_collide_within_a_second():
    """One-second ids made a second batch overwrite the first in _batches."""
    pipeline = VersionedEmbeddingPipeline(mock=True)
    batches = [
        pipeline.create_embedding_batch([Document(id=f"d{i}", content=str(i))])
        for i in range(200)
    ]
    ids = [b.batch_id for b in batches]
    assert len(set(ids)) == 200
    assert len(pipeline._batches) == 200
    assert all(pipeline._batches[b.batch_id] is b for b in batches)


def test_batch_and_manifest_ids_are_unique_even_at_a_frozen_clock(monkeypatch):
    import datetime as real_datetime

    import briefcase.rag.embedding_pipeline as module

    frozen = real_datetime.datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=real_datetime.timezone.utc)

    class FrozenDatetime(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(module, "datetime", FrozenDatetime)
    pipeline = VersionedEmbeddingPipeline(mock=True)
    doc = [Document(id="d1", content="hello")]
    first, second = pipeline.create_embedding_batch(doc), pipeline.create_embedding_batch(doc)
    assert first.batch_id != second.batch_id
    # Microsecond resolution, then a random suffix.
    assert first.batch_id.startswith("batch_20260102_030405_678901_")
    assert len(first.batch_id.rsplit("_", 1)[1]) == 6

    m1 = pipeline.create_manifest("idx", [first])
    m2 = pipeline.create_manifest("idx", [second])
    assert m1.manifest_id != m2.manifest_id
    assert m1.manifest_id.startswith("idx_20260102_030405_678901_unknown_")
    assert m2.parent_manifest_id == m1.manifest_id


def test_manifest_ids_do_not_collide_within_a_second():
    lakefs = VersionedClientDouble()
    pipeline = VersionedEmbeddingPipeline(lakefs_client=lakefs, repository="repo", mock=True)
    batch = pipeline.create_embedding_batch([Document(id="d1", content="hello")])
    manifests = [pipeline.create_manifest("idx", [batch]) for _ in range(100)]
    assert len({m.manifest_id for m in manifests}) == 100
    # A collision used to overwrite the earlier manifest's object in lakeFS.
    assert len(lakefs.uploads) == 100
    assert manifests[-1].parent_manifest_id == manifests[-2].manifest_id


# Manifest upload outcome

class _FailingUploadClient(VersionedClientDouble):
    def upload_object(self, path, data, content_type="application/octet-stream"):
        raise ConnectionError("lakeFS unreachable")


def test_manifest_upload_failure_is_surfaced_on_the_result(caplog):
    pipeline = VersionedEmbeddingPipeline(
        lakefs_client=_FailingUploadClient(), repository="repo", mock=True
    )
    batch = pipeline.create_embedding_batch([Document(id="d1", content="hello")])
    with caplog.at_level("WARNING", logger="briefcase"):
        manifest = pipeline.create_manifest("idx", [batch])  # does not raise

    assert manifest.uploaded is False
    assert manifest.upload_error == "ConnectionError: lakeFS unreachable"
    assert any("NOT stored" in r.getMessage() for r in caplog.records)
    # Still tracked in memory, as before.
    assert pipeline.get_latest_manifest("idx") is manifest
    # rebuild_index returns the same manifest type, so it reports it too.
    rebuilt = pipeline.rebuild_index("idx", [Document(id="d1", content="hello")])
    assert rebuilt.uploaded is False and rebuilt.upload_error


def test_manifest_upload_success_and_no_lakefs_are_distinguishable():
    stored = VersionedEmbeddingPipeline(
        lakefs_client=VersionedClientDouble(), repository="repo", mock=True
    )
    batch = stored.create_embedding_batch([Document(id="d1", content="hello")])
    manifest = stored.create_manifest("idx", [batch])
    assert manifest.uploaded is True and manifest.upload_error is None

    local = VersionedEmbeddingPipeline(mock=True)
    manifest = local.create_manifest(
        "idx", [local.create_embedding_batch([Document(id="d1", content="hello")])]
    )
    assert manifest.uploaded is None and manifest.upload_error is None


def test_upload_outcome_is_not_part_of_the_stored_manifest():
    import json

    from briefcase.rag.embedding_pipeline import EmbeddingManifest

    lakefs = VersionedClientDouble()
    pipeline = VersionedEmbeddingPipeline(lakefs_client=lakefs, repository="repo", mock=True)
    batch = pipeline.create_embedding_batch([Document(id="d1", content="hello")])
    manifest = pipeline.create_manifest("idx", [batch])
    stored = json.loads(lakefs.uploads[f"manifests/idx/{manifest.manifest_id}.json"])
    assert "uploaded" not in stored and "upload_error" not in stored
    assert "uploaded" not in manifest.to_dict()
    # Round-trips, and a reloaded manifest makes no claim about an upload.
    reloaded = EmbeddingManifest.from_json(manifest.to_json())
    assert reloaded == manifest and reloaded.uploaded is None
    assert EmbeddingManifest.from_dict({**manifest.to_dict(), "uploaded": True}).uploaded is None
