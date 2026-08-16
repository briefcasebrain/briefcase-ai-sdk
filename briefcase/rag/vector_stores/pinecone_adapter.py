"""
Pinecone vector store adapter with lakeFS version tracking.

Requires the ``pinecone`` client (v3 or later API):
``pip install briefcase-ai[rag-pinecone]`` or ``pip install pinecone``.
The import is lazy, so this module loads without pinecone installed and
fails with a clear error on construction.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from briefcase._logging import get_logger
from briefcase._otel import trace, HAS_OTEL
from briefcase.semantic_conventions.rag import (
    RAG_INDEX_NAME,
    RAG_INDEX_VERSION,
    RAG_RETRIEVED_COUNT,
)

logger = get_logger(__name__)

_PINECONE_INSTALL_HINT = (
    "pinecone is required. Install with "
    "'pip install briefcase-ai[rag-pinecone]' "
    "or 'pip install pinecone'."
)


def _field(obj: Any, key: str, default: Any = None) -> Any:
    """Read a field from a mapping-style or attribute-style API object."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class VersionedPineconeStore:
    """
    Pinecone vector store that stamps every vector with lakeFS provenance.

    Vectors are upserted with ``lakefs_repository``, ``lakefs_commit`` and
    ``lakefs_namespace`` metadata, and queries are filtered to the store's
    commit so results always come from one index version.

    Attributes:
        index_name: Pinecone index name.
        lakefs_repository: lakeFS repository the documents came from.
        lakefs_commit: lakeFS commit SHA the documents came from.
        namespace: Pinecone namespace (defaults to "default").
    """

    def __init__(
        self,
        index_name: str,
        lakefs_repository: str,
        lakefs_commit: str,
        api_key: Optional[str] = None,
        namespace: Optional[str] = None,
    ):
        """
        Initialize the store and connect to the index.

        Args:
            index_name: Pinecone index name.
            lakefs_repository: lakeFS repository name.
            lakefs_commit: lakeFS commit SHA.
            api_key: Pinecone API key; when omitted the client reads
                ``PINECONE_API_KEY`` from the environment.
            namespace: Pinecone namespace (defaults to "default").

        Raises:
            ImportError: the pinecone client is not installed.
        """
        self.index_name = index_name
        self.lakefs_repository = lakefs_repository
        self.lakefs_commit = lakefs_commit
        self.namespace = namespace or "default"

        try:
            from pinecone import Pinecone
        except ImportError as exc:  # pragma: no cover - exercised via stubs
            raise ImportError(_PINECONE_INSTALL_HINT) from exc

        self._client = Pinecone(api_key=api_key) if api_key else Pinecone()
        self.index = self._client.Index(index_name)

        self._tracer = trace.get_tracer(__name__) if HAS_OTEL else None

    def upsert_batch(
        self,
        embeddings: List[List[float]],
        document_ids: List[str],
        metadata: List[Dict[str, Any]],
    ) -> Dict[str, int]:
        """
        Upsert embeddings, enriching metadata with version info.

        Args:
            embeddings: Embedding vectors.
            document_ids: One document ID per vector.
            metadata: One metadata dict per vector.

        Returns:
            Dict with the upserted count.
        """
        span = self._tracer.start_span("rag.vector_store.upsert") if self._tracer else None

        try:
            if span:
                span.set_attribute("rag.vector_store.type", "pinecone")
                span.set_attribute("rag.vector_store.upsert_count", len(embeddings))
                span.set_attribute(RAG_INDEX_NAME, self.index_name)
                span.set_attribute(RAG_INDEX_VERSION, self.lakefs_commit)

            enriched_metadata = []
            for meta in metadata:
                meta_copy = meta.copy()
                meta_copy["lakefs_repository"] = self.lakefs_repository
                meta_copy["lakefs_commit"] = self.lakefs_commit
                meta_copy["lakefs_namespace"] = self.namespace
                enriched_metadata.append(meta_copy)

            vectors = [
                (doc_id, embedding, meta)
                for doc_id, embedding, meta in zip(document_ids, embeddings, enriched_metadata)
            ]

            if not vectors:
                return {"upserted_count": 0}

            response = self.index.upsert(vectors=vectors, namespace=self.namespace)

            upserted = _field(response, "upserted_count")
            return {"upserted_count": upserted if upserted is not None else len(vectors)}

        finally:
            if span:
                span.end()

    def query(
        self,
        query_embedding: List[float],
        top_k: int = 5,
        filter: Optional[Dict[str, Any]] = None,
        include_metadata: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Query the index, filtered to this store's lakeFS commit.

        Args:
            query_embedding: Query vector.
            top_k: Number of results to return.
            filter: Additional metadata filters.
            include_metadata: Whether to include metadata in results.

        Returns:
            List of matches with id, score and metadata.
        """
        span = self._tracer.start_span("rag.vector_store.query") if self._tracer else None

        try:
            if span:
                span.set_attribute("rag.vector_store.type", "pinecone")
                span.set_attribute("rag.vector_store.query_top_k", top_k)
                span.set_attribute(RAG_INDEX_VERSION, self.lakefs_commit)

            if filter is None:
                filter = {}
            filter["lakefs_commit"] = self.lakefs_commit

            results = self.index.query(
                vector=query_embedding,
                top_k=top_k,
                filter=filter,
                include_metadata=include_metadata,
                namespace=self.namespace,
            )

            matches = _field(results, "matches", []) or []

            if span:
                span.set_attribute(RAG_RETRIEVED_COUNT, len(matches))

            return [
                {
                    "id": _field(match, "id"),
                    "score": _field(match, "score"),
                    "metadata": _field(match, "metadata") or {},
                }
                for match in matches
            ]

        finally:
            if span:
                span.end()

    def delete(self, ids: List[str]) -> Dict[str, int]:
        """Delete vectors by ID."""
        self.index.delete(ids=ids, namespace=self.namespace)
        return {"deleted_count": len(ids)}

    def describe_index_stats(self) -> Dict[str, Any]:
        """Return index statistics: vector count, dimension and fullness."""
        stats = self.index.describe_index_stats()
        fullness = _field(stats, "index_fullness")
        return {
            "total_vector_count": _field(stats, "total_vector_count"),
            "dimension": _field(stats, "dimension"),
            "index_fullness": fullness if fullness is not None else 0.0,
        }
