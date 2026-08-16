"""Package-level tests for briefcase.rag.vector_stores.

The package must import on a bare install: the store clients import
their third-party libraries lazily, inside the constructors.
"""

from __future__ import annotations


def test_package_exports_all_stores():
    import briefcase.rag.vector_stores as vector_stores

    assert vector_stores.__all__ == [
        "VersionedChromaStore",
        "VersionedPineconeStore",
        "VersionedWeaviateStore",
    ]
    for name in vector_stores.__all__:
        assert getattr(vector_stores, name) is not None


def test_adapter_modules_import_without_clients():
    import briefcase.rag.vector_stores.chroma_adapter
    import briefcase.rag.vector_stores.pinecone_adapter
    import briefcase.rag.vector_stores.weaviate_adapter

    assert briefcase.rag.vector_stores.chroma_adapter.VersionedChromaStore
    assert briefcase.rag.vector_stores.pinecone_adapter.VersionedPineconeStore
    assert briefcase.rag.vector_stores.weaviate_adapter.VersionedWeaviateStore
