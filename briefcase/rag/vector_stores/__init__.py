"""
Vector store adapters with lakeFS version tracking.

Each adapter stamps every vector with the lakeFS repository and commit it
was built from, and filters queries to the same commit so retrieval results
always carry full version provenance.

Each adapter imports its client library lazily; installing the matching
extra is required only for the store you use:

* :class:`VersionedChromaStore` needs ``briefcase-ai[rag-chroma]``
* :class:`VersionedPineconeStore` needs ``briefcase-ai[rag-pinecone]``
* :class:`VersionedWeaviateStore` needs ``briefcase-ai[rag-weaviate]``
"""

from briefcase.rag.vector_stores.chroma_adapter import VersionedChromaStore
from briefcase.rag.vector_stores.pinecone_adapter import VersionedPineconeStore
from briefcase.rag.vector_stores.weaviate_adapter import VersionedWeaviateStore

__all__ = [
    "VersionedChromaStore",
    "VersionedPineconeStore",
    "VersionedWeaviateStore",
]
