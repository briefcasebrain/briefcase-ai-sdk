"""Tests for briefcase.rag.vector_stores.pinecone_adapter.VersionedPineconeStore.

The pinecone client is optional and not installed in CI; every test
injects a stub ``pinecone`` module (v3-generation ``Pinecone`` class API)
with ``monkeypatch.setitem(sys.modules, ...)`` so the adapter's real code
paths run against an in-memory fake index.
"""

from __future__ import annotations

import sys
import types

import pytest

from briefcase.rag.vector_stores.pinecone_adapter import VersionedPineconeStore


class FakeUpsertResponse:
    def __init__(self, upserted_count=None):
        if upserted_count is not None:
            self.upserted_count = upserted_count


class FakeMatch:
    def __init__(self, mid, score, metadata):
        self.id = mid
        self.score = score
        self.metadata = metadata


class FakeStats:
    def __init__(self, include_fullness=True):
        self.total_vector_count = 10
        self.dimension = 3
        if include_fullness:
            self.index_fullness = 0.5


class FakePineconeIndex:
    def __init__(self, name):
        self.name = name
        self.vectors = {}
        self.last_filter = None
        self.last_namespace = None
        self.deleted = []
        self.include_fullness = True
        self.upsert_response_count = True

    def upsert(self, vectors, namespace):
        self.last_namespace = namespace
        for doc_id, embedding, meta in vectors:
            self.vectors[(namespace, doc_id)] = (embedding, meta)
        if self.upsert_response_count:
            return FakeUpsertResponse(upserted_count=len(vectors))
        return FakeUpsertResponse(upserted_count=None)

    def query(self, vector, top_k, filter, include_metadata, namespace):
        self.last_filter = filter
        self.last_namespace = namespace
        keys = [k for k in self.vectors if k[0] == namespace][:top_k]
        return {
            "matches": [
                FakeMatch(doc_id, 0.95 - 0.05 * i, self.vectors[(ns, doc_id)][1])
                for i, (ns, doc_id) in enumerate(keys)
            ]
        }

    def delete(self, ids, namespace):
        self.deleted.append((ids, namespace))
        for doc_id in ids:
            self.vectors.pop((namespace, doc_id), None)

    def describe_index_stats(self):
        return FakeStats(include_fullness=self.include_fullness)


@pytest.fixture
def pinecone_stub(monkeypatch):
    """Install a stub pinecone module and return it."""
    module = types.ModuleType("pinecone")
    module.api_keys = []
    module.indexes = {}

    class Pinecone:
        def __init__(self, api_key=None):
            module.api_keys.append(api_key)

        def Index(self, name):
            if name not in module.indexes:
                module.indexes[name] = FakePineconeIndex(name)
            return module.indexes[name]

    module.Pinecone = Pinecone
    monkeypatch.setitem(sys.modules, "pinecone", module)
    return module


def _make_store(**kwargs):
    defaults = {
        "index_name": "test_index",
        "lakefs_repository": "test-repo",
        "lakefs_commit": "abc123",
    }
    defaults.update(kwargs)
    return VersionedPineconeStore(**defaults)


def test_missing_pinecone_raises_import_error_naming_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "pinecone", None)
    with pytest.raises(ImportError, match="rag-pinecone"):
        _make_store()


def test_init_defaults(pinecone_stub):
    store = _make_store()
    assert store.index_name == "test_index"
    assert store.namespace == "default"
    assert pinecone_stub.api_keys == [None]


def test_init_with_namespace(pinecone_stub):
    store = _make_store(namespace="test_namespace")
    assert store.namespace == "test_namespace"


def test_init_with_api_key(pinecone_stub):
    _make_store(api_key="test_key")
    assert pinecone_stub.api_keys == ["test_key"]


def test_init_failure_propagates(monkeypatch):
    module = types.ModuleType("pinecone")

    class Pinecone:
        def __init__(self, api_key=None):
            raise RuntimeError("init failed")

    module.Pinecone = Pinecone
    monkeypatch.setitem(sys.modules, "pinecone", module)
    with pytest.raises(RuntimeError, match="init failed"):
        _make_store(api_key="k")


def test_upsert_batch_returns_count(pinecone_stub):
    store = _make_store()
    count = store.upsert_batch(
        embeddings=[[0.1, 0.2], [0.3, 0.4]],
        document_ids=["id_1", "id_2"],
        metadata=[{"source": "file1"}, {"source": "file2"}],
    )
    assert count == {"upserted_count": 2}


def test_upsert_batch_enriches_metadata_with_version(pinecone_stub):
    store = _make_store(namespace="ns1")
    store.upsert_batch(
        embeddings=[[0.1, 0.2]],
        document_ids=["id_1"],
        metadata=[{"source": "file1"}],
    )
    _, meta = pinecone_stub.indexes["test_index"].vectors[("ns1", "id_1")]
    assert meta["lakefs_repository"] == "test-repo"
    assert meta["lakefs_commit"] == "abc123"
    assert meta["lakefs_namespace"] == "ns1"
    assert meta["source"] == "file1"


def test_upsert_batch_empty(pinecone_stub):
    store = _make_store()
    count = store.upsert_batch(embeddings=[], document_ids=[], metadata=[])
    assert count == {"upserted_count": 0}


def test_upsert_batch_large_batch(pinecone_stub):
    store = _make_store()
    count = store.upsert_batch(
        embeddings=[[0.1 * i, 0.2 * i] for i in range(100)],
        document_ids=[f"id_{i}" for i in range(100)],
        metadata=[{} for _ in range(100)],
    )
    assert count == {"upserted_count": 100}


def test_upsert_batch_falls_back_when_response_lacks_count(pinecone_stub):
    store = _make_store()
    pinecone_stub.indexes["test_index"].upsert_response_count = False
    count = store.upsert_batch(
        embeddings=[[0.1]],
        document_ids=["id_1"],
        metadata=[{}],
    )
    assert count == {"upserted_count": 1}


def test_query_returns_matches(pinecone_stub):
    store = _make_store()
    store.upsert_batch(
        embeddings=[[0.1, 0.2]],
        document_ids=["id_1"],
        metadata=[{"source": "s1"}],
    )
    results = store.query([0.1, 0.2], top_k=2)
    assert results[0]["id"] == "id_1"
    assert results[0]["score"] == 0.95
    assert results[0]["metadata"]["source"] == "s1"


def test_query_filters_to_store_commit(pinecone_stub):
    store = _make_store()
    store.query([0.1, 0.2], top_k=2)
    assert pinecone_stub.indexes["test_index"].last_filter == {"lakefs_commit": "abc123"}


def test_query_merges_caller_filter(pinecone_stub):
    store = _make_store()
    store.query([0.1, 0.2], top_k=2, filter={"source": "s1"})
    assert pinecone_stub.indexes["test_index"].last_filter == {
        "source": "s1",
        "lakefs_commit": "abc123",
    }


def test_query_top_k_respected(pinecone_stub):
    store = _make_store()
    store.upsert_batch(
        embeddings=[[0.1], [0.2], [0.3]],
        document_ids=["id_1", "id_2", "id_3"],
        metadata=[{}, {}, {}],
    )
    results = store.query([0.1], top_k=2)
    assert len(results) == 2


def test_delete_returns_count(pinecone_stub):
    store = _make_store()
    store.upsert_batch(embeddings=[[0.1]], document_ids=["id_1"], metadata=[{}])
    result = store.delete(ids=["id_1"])
    assert result == {"deleted_count": 1}
    assert pinecone_stub.indexes["test_index"].deleted == [(["id_1"], "default")]


def test_delete_nonexistent_id(pinecone_stub):
    store = _make_store()
    result = store.delete(ids=["nonexistent"])
    assert result == {"deleted_count": 1}


def test_describe_index_stats(pinecone_stub):
    store = _make_store()
    stats = store.describe_index_stats()
    assert stats["total_vector_count"] == 10
    assert stats["dimension"] == 3
    assert stats["index_fullness"] == 0.5


def test_describe_index_stats_defaults_fullness(pinecone_stub):
    store = _make_store()
    pinecone_stub.indexes["test_index"].include_fullness = False
    stats = store.describe_index_stats()
    assert stats["index_fullness"] == 0.0


def test_namespace_isolation(pinecone_stub):
    store1 = _make_store(namespace="ns1")
    store2 = _make_store(namespace="ns2")
    store1.upsert_batch(embeddings=[[0.1]], document_ids=["id_1"], metadata=[{}])
    assert store1.query([0.1], top_k=5) != []
    assert store2.query([0.1], top_k=5) == []


def test_multiple_operations_sequence(pinecone_stub):
    store = _make_store()
    assert store.upsert_batch(
        embeddings=[[0.1], [0.2]],
        document_ids=["id_1", "id_2"],
        metadata=[{}, {}],
    ) == {"upserted_count": 2}
    assert len(store.query([0.1], top_k=2)) == 2
    assert store.delete(ids=["id_1"]) == {"deleted_count": 1}
