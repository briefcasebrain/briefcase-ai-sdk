"""
Chroma vector store adapter with lakeFS version tracking.

Requires ``chromadb``: ``pip install briefcase-ai[rag-chroma]`` or
``pip install chromadb``. The import is lazy, so this module loads without
chromadb installed and fails with a clear error on construction.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from briefcase._logging import get_logger
from briefcase._otel import trace, HAS_OTEL
from briefcase.semantic_conventions.rag import (
    RAG_INDEX_VERSION,
    RAG_RETRIEVED_COUNT,
)

logger = get_logger(__name__)

_CHROMA_INSTALL_HINT = (
    "chromadb is required. Install with "
    "'pip install briefcase-ai[rag-chroma]' "
    "or 'pip install chromadb'."
)


class VersionedChromaStore:
    """
    Chroma vector store that stamps every vector with lakeFS provenance.

    Vectors are written with ``lakefs_repository`` and ``lakefs_commit``
    metadata, and queries are filtered to the store's commit so results
    always come from one index version.

    Attributes:
        collection_name: Chroma collection name.
        lakefs_repository: lakeFS repository the documents came from.
        lakefs_commit: lakeFS commit SHA the documents came from.
    """

    def __init__(
        self,
        collection_name: str,
        lakefs_repository: str,
        lakefs_commit: str,
        persist_directory: Optional[str] = None,
    ):
        """
        Initialize the store and open (or create) the collection.

        Args:
            collection_name: Chroma collection name.
            lakefs_repository: lakeFS repository name.
            lakefs_commit: lakeFS commit SHA.
            persist_directory: Directory for a persistent Chroma client;
                omit for an in-memory client.

        Raises:
            ImportError: chromadb is not installed.
        """
        self.collection_name = collection_name
        self.lakefs_repository = lakefs_repository
        self.lakefs_commit = lakefs_commit

        try:
            import chromadb
        except ImportError as exc:  # pragma: no cover - exercised via stubs
            raise ImportError(_CHROMA_INSTALL_HINT) from exc

        if persist_directory:
            self.client = chromadb.PersistentClient(path=persist_directory)
        else:
            self.client = chromadb.Client()

        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata={"lakefs_repository": lakefs_repository},
        )

        self._tracer = trace.get_tracer(__name__) if HAS_OTEL else None

    def add_batch(
        self,
        embeddings: List[List[float]],
        document_ids: List[str],
        documents: List[str],
        metadata: List[Dict[str, Any]],
    ) -> Dict[str, int]:
        """Add embeddings in batch, enriching metadata with version info."""
        span = self._tracer.start_span("rag.vector_store.add_batch") if self._tracer else None

        try:
            if span:
                span.set_attribute("rag.vector_store.type", "chroma")
                span.set_attribute("rag.vector_store.add_count", len(embeddings))
                span.set_attribute(RAG_INDEX_VERSION, self.lakefs_commit)

            enriched_metadata = []
            for meta in metadata:
                meta_copy = meta.copy()
                meta_copy["lakefs_repository"] = self.lakefs_repository
                meta_copy["lakefs_commit"] = self.lakefs_commit
                enriched_metadata.append(meta_copy)

            self.collection.add(
                embeddings=embeddings,
                documents=documents,
                metadatas=enriched_metadata,
                ids=document_ids,
            )

            return {"added_count": len(embeddings)}

        finally:
            if span:
                span.end()

    def query(
        self,
        query_embeddings: List[List[float]],
        n_results: int = 5,
        where: Optional[Dict[str, Any]] = None,
        include: Optional[List[str]] = None,
    ) -> Dict[str, List[Any]]:
        """Query the collection, filtered to this store's lakeFS commit."""
        span = self._tracer.start_span("rag.vector_store.query") if self._tracer else None

        try:
            if span:
                span.set_attribute("rag.vector_store.type", "chroma")
                span.set_attribute("rag.vector_store.query_top_k", n_results)

            version_where: Dict[str, Any] = {"lakefs_commit": self.lakefs_commit}
            if where:
                combined_where: Dict[str, Any] = {"$and": [version_where, where]}
            else:
                combined_where = version_where

            results = self.collection.query(
                query_embeddings=query_embeddings,
                n_results=n_results,
                where=combined_where,
                include=include or ["documents", "metadatas", "distances"],
            )

            if span:
                result_count = len(results.get("ids", [[]])[0])
                span.set_attribute(RAG_RETRIEVED_COUNT, result_count)

            return results

        finally:
            if span:
                span.end()

    def delete(self, ids: List[str]) -> Dict[str, int]:
        """Delete vectors by ID."""
        self.collection.delete(ids=ids)
        return {"deleted_count": len(ids)}

    def count(self) -> int:
        """Return the number of vectors in the collection."""
        return self.collection.count()

    def peek(self, limit: int = 10) -> Dict[str, List[Any]]:
        """Return up to ``limit`` entries from the collection."""
        return self.collection.peek(limit=limit)
