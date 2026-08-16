"""Tests for briefcase.rag.vector_stores.chroma_adapter.VersionedChromaStore.

chromadb is optional and not installed in CI; every test injects a stub
``chromadb`` module with ``monkeypatch.setitem(sys.modules, ...)`` so the
adapter's real code paths run against an in-memory fake collection.
"""

from __future__ import annotations

import sys
import types

import pytest

from briefcase.rag.vector_stores.chroma_adapter import VersionedChromaStore


class FakeChromaCollection:
    """In-memory stand-in for a chromadb collection."""

    def __init__(self, name, metadata=None):
        self.name = name
        self.metadata = metadata
        self.ids = []
        self.embeddings = []
        self.documents = []
        self.metadatas = []
        self.add_calls = []
        self.delete_calls = []

    def add(self, embeddings, documents, metadatas, ids):
        self.add_calls.append((embeddings, documents, metadatas, ids))
        self.ids.extend(ids)
        self.embeddings.extend(embeddings)
        self.documents.extend(documents)
        self.metadatas.extend(metadatas)

    def query(self, query_embeddings, n_results, where, include):
        self.last_where = where
        k = min(n_results, len(self.ids))
        return {
            "ids": [self.ids[:k]],
            "distances": [[0.1 * (i + 1) for i in range(k)]],
            "documents": [self.documents[:k]],
            "metadatas": [self.metadatas[:k]],
        }

    def delete(self, ids):
        self.delete_calls.append(ids)
        for doc_id in ids:
            if doc_id in self.ids:
                idx = self.ids.index(doc_id)
                for lst in (self.ids, self.embeddings, self.documents, self.metadatas):
                    del lst[idx]

    def count(self):
        return len(self.ids)

    def peek(self, limit=10):
        k = min(limit, len(self.ids))
        return {
            "ids": self.ids[:k],
            "documents": self.documents[:k],
            "metadatas": self.metadatas[:k],
        }


class FakeChromaClient:
    def __init__(self):
        self.collections = {}

    def get_or_create_collection(self, name, metadata=None):
        if name not in self.collections:
            self.collections[name] = FakeChromaCollection(name, metadata)
        return self.collections[name]


@pytest.fixture
def chroma_stub(monkeypatch):
    """Install a stub chromadb module and return it."""
    module = types.ModuleType("chromadb")
    module.persistent_paths = []

    def _client():
        return FakeChromaClient()

    def _persistent_client(path):
        module.persistent_paths.append(path)
        return FakeChromaClient()

    module.Client = _client
    module.PersistentClient = _persistent_client
    monkeypatch.setitem(sys.modules, "chromadb", module)
    return module


def _make_store(**kwargs):
    defaults = {
        "collection_name": "test_collection",
        "lakefs_repository": "test-repo",
        "lakefs_commit": "abc123",
    }
    defaults.update(kwargs)
    return VersionedChromaStore(**defaults)


def test_missing_chromadb_raises_import_error_naming_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "chromadb", None)
    with pytest.raises(ImportError, match="rag-chroma"):
        _make_store()


def test_init_stores_version_fields(chroma_stub):
    store = _make_store()
    assert store.collection_name == "test_collection"
    assert store.lakefs_repository == "test-repo"
    assert store.lakefs_commit == "abc123"


def test_init_with_persist_directory(chroma_stub):
    store = _make_store(persist_directory="/tmp/chroma")
    assert store.collection_name == "test_collection"
    assert chroma_stub.persistent_paths == ["/tmp/chroma"]


def test_init_failure_propagates(monkeypatch):
    module = types.ModuleType("chromadb")

    def _boom():
        raise RuntimeError("boom")

    module.Client = _boom
    module.PersistentClient = lambda path: _boom()
    monkeypatch.setitem(sys.modules, "chromadb", module)
    with pytest.raises(RuntimeError, match="boom"):
        _make_store()


def test_add_batch_returns_count(chroma_stub):
    store = _make_store()
    count = store.add_batch(
        embeddings=[[0.1, 0.2], [0.3, 0.4]],
        document_ids=["doc_1", "doc_2"],
        documents=["content 1", "content 2"],
        metadata=[{"source": "file1"}, {"source": "file2"}],
    )
    assert count == {"added_count": 2}


def test_add_batch_enriches_metadata_with_version(chroma_stub):
    store = _make_store()
    store.add_batch(
        embeddings=[[0.1, 0.2]],
        document_ids=["doc_1"],
        documents=["content 1"],
        metadata=[{"source": "file1"}],
    )
    _, _, metadatas, _ = store.collection.add_calls[0]
    assert metadatas[0]["lakefs_repository"] == "test-repo"
    assert metadatas[0]["lakefs_commit"] == "abc123"
    assert metadatas[0]["source"] == "file1"


def test_add_batch_does_not_mutate_caller_metadata(chroma_stub):
    store = _make_store()
    meta = {"source": "file1"}
    store.add_batch(
        embeddings=[[0.1, 0.2]],
        document_ids=["doc_1"],
        documents=["content 1"],
        metadata=[meta],
    )
    assert meta == {"source": "file1"}


def test_add_batch_empty(chroma_stub):
    store = _make_store()
    count = store.add_batch(embeddings=[], document_ids=[], documents=[], metadata=[])
    assert count == {"added_count": 0}


def test_query_returns_structure(chroma_stub):
    store = _make_store()
    store.add_batch(
        embeddings=[[0.1, 0.2], [0.3, 0.4]],
        document_ids=["doc_1", "doc_2"],
        documents=["content 1", "content 2"],
        metadata=[{}, {}],
    )
    results = store.query(query_embeddings=[[0.1, 0.2]], n_results=2)
    assert isinstance(results, dict)
    assert results["ids"][0] == ["doc_1", "doc_2"]


def test_query_filters_to_store_commit(chroma_stub):
    store = _make_store()
    store.query(query_embeddings=[[0.1, 0.2]], n_results=2)
    assert store.collection.last_where == {"lakefs_commit": "abc123"}


def test_query_combines_caller_filter_with_version_filter(chroma_stub):
    store = _make_store()
    store.query(query_embeddings=[[0.1, 0.2]], n_results=2, where={"source": "s1"})
    assert store.collection.last_where == {
        "$and": [{"lakefs_commit": "abc123"}, {"source": "s1"}]
    }


def test_query_top_k_respected(chroma_stub):
    store = _make_store()
    store.add_batch(
        embeddings=[[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]],
        document_ids=["doc_1", "doc_2", "doc_3"],
        documents=["c1", "c2", "c3"],
        metadata=[{}, {}, {}],
    )
    results = store.query(query_embeddings=[[0.1, 0.2]], n_results=2)
    assert len(results["ids"][0]) == 2


def test_delete_returns_count(chroma_stub):
    store = _make_store()
    store.add_batch(
        embeddings=[[0.1, 0.2], [0.3, 0.4]],
        document_ids=["doc_1", "doc_2"],
        documents=["c1", "c2"],
        metadata=[{}, {}],
    )
    result = store.delete(ids=["doc_1"])
    assert result == {"deleted_count": 1}
    assert store.count() == 1


def test_delete_nonexistent_id(chroma_stub):
    store = _make_store()
    result = store.delete(ids=["nonexistent"])
    assert isinstance(result, dict)


def test_count_initially_zero(chroma_stub):
    store = _make_store()
    assert store.count() == 0


def test_count_after_add(chroma_stub):
    store = _make_store()
    store.add_batch(
        embeddings=[[0.1, 0.2], [0.3, 0.4]],
        document_ids=["doc_1", "doc_2"],
        documents=["c1", "c2"],
        metadata=[{}, {}],
    )
    assert store.count() == 2


def test_peek_empty(chroma_stub):
    store = _make_store()
    result = store.peek(limit=10)
    assert result["ids"] == []


def test_peek_with_data_respects_limit(chroma_stub):
    store = _make_store()
    store.add_batch(
        embeddings=[[0.1, 0.2], [0.3, 0.4]],
        document_ids=["doc_1", "doc_2"],
        documents=["c1", "c2"],
        metadata=[{}, {}],
    )
    result = store.peek(limit=1)
    assert result["ids"] == ["doc_1"]


def test_multiple_operations_sequence(chroma_stub):
    store = _make_store()
    assert store.add_batch(
        embeddings=[[0.1, 0.2], [0.3, 0.4]],
        document_ids=["doc_1", "doc_2"],
        documents=["c1", "c2"],
        metadata=[{}, {}],
    ) == {"added_count": 2}
    assert isinstance(store.query(query_embeddings=[[0.1, 0.2]], n_results=2), dict)
    assert isinstance(store.delete(ids=["doc_1"]), dict)


def test_store_isolates_collections(chroma_stub):
    store1 = _make_store(collection_name="collection_1")
    store2 = _make_store(collection_name="collection_2")
    store1.add_batch(
        embeddings=[[0.1, 0.2]],
        document_ids=["doc_1"],
        documents=["c1"],
        metadata=[{}],
    )
    assert store1.count() == 1
    assert store2.count() == 0
