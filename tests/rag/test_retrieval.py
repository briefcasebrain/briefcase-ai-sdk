"""InstrumentedRetriever delegates to a real store and never invents results."""

from types import SimpleNamespace

import pytest

from briefcase.rag.retrieval import InstrumentedRetriever, RetrievalResult


class _Lakefs:
    def get_commit(self):
        return "c0ffee"


def test_no_store_raises_instead_of_returning_mock_documents():
    retriever = InstrumentedRetriever(None, _Lakefs(), "repo")
    with pytest.raises(RuntimeError, match="no vector_store"):
        retriever.retrieve("q")


def test_unsupported_store_raises_type_error():
    with pytest.raises(TypeError):
        InstrumentedRetriever(object(), None, "repo").retrieve("q")


def test_delegates_to_callable_and_stamps_commit():
    calls = []

    def search(query, top_k):
        calls.append((query, top_k))
        return [
            {"id": "a", "content": "alpha", "score": 0.9, "metadata": {"k": 1}},
            {"id": "b", "text": "beta", "score": 0.5},
            {"document_id": "c", "content": "gamma", "score": 0.8,
             "metadata": {"commit": "pinned"}},
        ]

    results = InstrumentedRetriever(search, _Lakefs(), "repo").retrieve("hello", top_k=3)

    assert calls == [("hello", 3)]
    assert [r.document_id for r in results] == ["a", "c"]  # 0.5 < threshold
    assert [r.rank for r in results] == [0, 1]
    assert results[0].content == "alpha"
    assert results[0].document_version == "c0ffee"
    assert results[1].document_version == "pinned"
    assert not any("Mock" in r.content for r in results)


def test_delegates_to_retrieve_method_and_passes_results_through():
    ready = RetrievalResult("x", "body", 0.99, 0, "", {})
    store = SimpleNamespace(retrieve=lambda query, top_k: [ready])
    results = InstrumentedRetriever(store, None, "repo").retrieve("q")
    assert results == [ready]
    assert results[0].document_version == "unknown"


def test_langchain_style_store_and_error_propagation():
    doc = SimpleNamespace(page_content="text", metadata={"id": "d1"})

    class Store:
        def similarity_search_with_score(self, query, k):
            return [(doc, 0.75)]

    results = InstrumentedRetriever(Store(), _Lakefs(), "repo").retrieve("q", top_k=1)
    assert (results[0].document_id, results[0].content, results[0].score) == ("d1", "text", 0.75)

    def boom(query, top_k):
        raise ConnectionError("store down")

    with pytest.raises(ConnectionError):
        InstrumentedRetriever(boom, None, "repo").retrieve("q")

    with pytest.raises(ValueError, match="no score"):
        InstrumentedRetriever(lambda q, k: [{"id": "a"}], None, "repo").retrieve("q")
