"""Tests for briefcase.rag.vector_stores.weaviate_adapter.VersionedWeaviateStore.

weaviate-client is optional and not installed in CI; every test injects a
stub ``weaviate`` module (v3-generation API) with
``monkeypatch.setitem(sys.modules, ...)`` so the adapter's real code paths
run against an in-memory fake client.
"""

from __future__ import annotations

import sys
import types

import pytest

from briefcase.rag.vector_stores.weaviate_adapter import VersionedWeaviateStore


class FakeBatch:
    def __init__(self):
        self.items = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def add_data_object(self, data_object, class_name, vector, uuid):
        self.items.append((data_object, class_name, vector, uuid))


class FakeQueryBuilder:
    def __init__(self, client):
        self._client = client
        self._class_name = None
        self._limit = None
        self.last_where = None

    def get(self, class_name, _props):
        self._class_name = class_name
        return self

    def with_near_vector(self, _value):
        return self

    def with_limit(self, value):
        self._limit = value
        return self

    def with_where(self, value):
        self.last_where = value
        return self

    def with_additional(self, _value):
        return self

    def do(self):
        objects = [
            {
                "document_id": props["document_id"],
                "text": props["text"],
                "lakefs_commit": props["lakefs_commit"],
                "lakefs_repository": props["lakefs_repository"],
                "_additional": {"id": uuid, "distance": 0.05 * (i + 1)},
            }
            for i, (props, class_name, _vector, uuid) in enumerate(self._client.batch.items)
            if class_name == self._class_name
        ][: self._limit]
        return {"data": {"Get": {self._class_name: objects}}}


class FakeWeaviateClient:
    def __init__(self, url):
        self.url = url
        self.batch = FakeBatch()
        self.query = FakeQueryBuilder(self)
        self.deleted = []
        self.data_object = types.SimpleNamespace(
            delete=lambda document_id, class_name=None: self.deleted.append(
                (document_id, class_name)
            )
        )
        self.schema = types.SimpleNamespace(
            get=lambda class_name: {"class": class_name, "properties": []}
        )


@pytest.fixture
def weaviate_stub(monkeypatch):
    """Install a stub weaviate module and return it."""
    module = types.ModuleType("weaviate")
    module.clients = []
    module.auth_configs = []

    def _auth(api_key):
        return {"api_key": api_key}

    def _client(url, auth_client_secret=None):
        module.auth_configs.append(auth_client_secret)
        client = FakeWeaviateClient(url)
        module.clients.append(client)
        return client

    module.AuthApiKey = _auth
    module.Client = _client
    monkeypatch.setitem(sys.modules, "weaviate", module)
    return module


def _make_store(**kwargs):
    defaults = {
        "url": "http://localhost:8080",
        "class_name": "DocClass",
        "lakefs_repository": "test-repo",
        "lakefs_commit": "abc123",
    }
    defaults.update(kwargs)
    return VersionedWeaviateStore(**defaults)


def test_missing_weaviate_raises_import_error_naming_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "weaviate", None)
    with pytest.raises(ImportError, match="rag-weaviate"):
        _make_store()


def test_init_stores_fields(weaviate_stub):
    store = _make_store()
    assert store.url == "http://localhost:8080"
    assert store.class_name == "DocClass"
    assert weaviate_stub.auth_configs == [None]


def test_init_with_api_key_builds_auth(weaviate_stub):
    _make_store(api_key="k")
    assert weaviate_stub.auth_configs == [{"api_key": "k"}]


def test_init_failure_propagates(monkeypatch):
    module = types.ModuleType("weaviate")
    module.AuthApiKey = lambda api_key: None

    def _client(url, auth_client_secret=None):
        raise RuntimeError("weaviate client failed")

    module.Client = _client
    monkeypatch.setitem(sys.modules, "weaviate", module)
    with pytest.raises(RuntimeError, match="weaviate client failed"):
        _make_store()


def test_add_batch_returns_count(weaviate_stub):
    store = _make_store()
    added = store.add_batch(
        embeddings=[[0.1, 0.2], [0.3, 0.4]],
        document_ids=["doc_1", "doc_2"],
        texts=["hello", "world"],
        metadata=[{}, {}],
    )
    assert added == {"added_count": 2}


def test_add_batch_enriches_properties_with_version(weaviate_stub):
    store = _make_store()
    store.add_batch(
        embeddings=[[0.1, 0.2]],
        document_ids=["doc_1"],
        texts=["hello"],
        metadata=[{"source": "s1"}],
    )
    props, class_name, vector, uuid = store.client.batch.items[0]
    assert props["lakefs_repository"] == "test-repo"
    assert props["lakefs_commit"] == "abc123"
    assert props["document_id"] == "doc_1"
    assert props["text"] == "hello"
    assert props["source"] == "s1"
    assert class_name == "DocClass"
    assert vector == [0.1, 0.2]
    assert uuid == "doc_1"


def test_add_batch_empty(weaviate_stub):
    store = _make_store()
    added = store.add_batch(embeddings=[], document_ids=[], texts=[], metadata=[])
    assert added == {"added_count": 0}


def test_add_batch_large_batch(weaviate_stub):
    store = _make_store()
    added = store.add_batch(
        embeddings=[[0.1 * i] for i in range(50)],
        document_ids=[f"doc_{i}" for i in range(50)],
        texts=[f"text {i}" for i in range(50)],
        metadata=[{} for _ in range(50)],
    )
    assert added == {"added_count": 50}
    assert len(store.client.batch.items) == 50


def test_query_returns_results(weaviate_stub):
    store = _make_store()
    store.add_batch(
        embeddings=[[0.1, 0.2]],
        document_ids=["doc_1"],
        texts=["hello"],
        metadata=[{}],
    )
    results = store.query([0.1, 0.2], top_k=2)
    assert results[0]["id"] == "doc_1"
    assert results[0]["text"] == "hello"
    assert results[0]["metadata"]["lakefs_commit"] == "abc123"


def test_query_filters_to_store_commit(weaviate_stub):
    store = _make_store()
    store.query([0.1, 0.2], top_k=2)
    assert store.client.query.last_where == {
        "path": ["lakefs_commit"],
        "operator": "Equal",
        "valueText": "abc123",
    }


def test_query_combines_caller_filter(weaviate_stub):
    store = _make_store()
    caller_filter = {"path": ["source"], "operator": "Equal", "valueText": "s1"}
    store.query([0.1, 0.2], top_k=2, where_filter=caller_filter)
    combined = store.client.query.last_where
    assert combined["operator"] == "And"
    assert caller_filter in combined["operands"]


def test_query_top_k_respected(weaviate_stub):
    store = _make_store()
    store.add_batch(
        embeddings=[[0.1], [0.2], [0.3]],
        document_ids=["doc_1", "doc_2", "doc_3"],
        texts=["a", "b", "c"],
        metadata=[{}, {}, {}],
    )
    results = store.query([0.1], top_k=2)
    assert len(results) == 2


def test_delete_by_id_returns_true(weaviate_stub):
    store = _make_store()
    assert store.delete_by_id("doc_1") is True
    assert store.client.deleted == [("doc_1", "DocClass")]


def test_delete_by_id_failure_returns_false(weaviate_stub):
    store = _make_store()

    def _boom(document_id, class_name=None):
        raise RuntimeError("delete fail")

    store.client.data_object.delete = _boom
    assert store.delete_by_id("doc_1") is False


def test_get_schema(weaviate_stub):
    store = _make_store()
    schema = store.get_schema()
    assert schema["class"] == "DocClass"


def test_get_schema_failure_returns_empty(weaviate_stub):
    store = _make_store()

    def _boom(class_name):
        raise RuntimeError("schema fail")

    store.client.schema.get = _boom
    assert store.get_schema() == {}


def test_different_classes_isolated(weaviate_stub):
    store1 = _make_store(class_name="ClassA")
    store2 = _make_store(class_name="ClassB")
    store1.add_batch(
        embeddings=[[0.1]],
        document_ids=["doc_1"],
        texts=["a"],
        metadata=[{}],
    )
    assert len(store1.query([0.1], top_k=5)) == 1
    assert store2.query([0.1], top_k=5) == []


def test_multiple_operations_sequence(weaviate_stub):
    store = _make_store()
    assert store.add_batch(
        embeddings=[[0.1], [0.2]],
        document_ids=["doc_1", "doc_2"],
        texts=["a", "b"],
        metadata=[{}, {}],
    ) == {"added_count": 2}
    assert len(store.query([0.1], top_k=2)) == 2
    assert store.delete_by_id("doc_1") is True
