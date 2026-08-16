"""
Weaviate vector store adapter with lakeFS version tracking.

Requires the v3-generation ``weaviate-client`` (this adapter uses the
GraphQL query builder API removed in weaviate-client 4):
``pip install briefcase-ai[rag-weaviate]`` or
``pip install 'weaviate-client>=3,<4'``. The import is lazy, so this
module loads without weaviate installed and fails with a clear error on
construction.
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

_WEAVIATE_INSTALL_HINT = (
    "weaviate-client (v3) is required. Install with "
    "'pip install briefcase-ai[rag-weaviate]' "
    "or 'pip install \"weaviate-client>=3,<4\"'."
)


class VersionedWeaviateStore:
    """
    Weaviate vector store that stamps every object with lakeFS provenance.

    Objects are written with ``lakefs_repository`` and ``lakefs_commit``
    properties, and queries are filtered to the store's commit so results
    always come from one index version.

    Attributes:
        url: Weaviate server URL.
        class_name: Weaviate class objects are stored under.
        lakefs_repository: lakeFS repository the documents came from.
        lakefs_commit: lakeFS commit SHA the documents came from.
    """

    def __init__(
        self,
        url: str,
        class_name: str,
        lakefs_repository: str,
        lakefs_commit: str,
        api_key: Optional[str] = None,
    ):
        """
        Initialize the store and connect to the Weaviate server.

        Args:
            url: Weaviate server URL.
            class_name: Weaviate class name.
            lakefs_repository: lakeFS repository name.
            lakefs_commit: lakeFS commit SHA.
            api_key: Optional API key for authentication.

        Raises:
            ImportError: weaviate-client is not installed.
        """
        self.url = url
        self.class_name = class_name
        self.lakefs_repository = lakefs_repository
        self.lakefs_commit = lakefs_commit

        try:
            import weaviate
        except ImportError as exc:  # pragma: no cover - exercised via stubs
            raise ImportError(_WEAVIATE_INSTALL_HINT) from exc

        auth_config = weaviate.AuthApiKey(api_key=api_key) if api_key else None
        self.client = weaviate.Client(url=url, auth_client_secret=auth_config)

        self._tracer = trace.get_tracer(__name__) if HAS_OTEL else None

    def add_batch(
        self,
        embeddings: List[List[float]],
        document_ids: List[str],
        texts: List[str],
        metadata: List[Dict[str, Any]],
    ) -> Dict[str, int]:
        """Add objects in batch, enriching properties with version info."""
        span = self._tracer.start_span("rag.vector_store.add_batch") if self._tracer else None

        try:
            if span:
                span.set_attribute("rag.vector_store.type", "weaviate")
                span.set_attribute("rag.vector_store.add_count", len(embeddings))
                span.set_attribute(RAG_INDEX_VERSION, self.lakefs_commit)

            with self.client.batch as batch:
                for doc_id, embedding, text, meta in zip(document_ids, embeddings, texts, metadata):
                    properties = {
                        "document_id": doc_id,
                        "text": text,
                        "lakefs_repository": self.lakefs_repository,
                        "lakefs_commit": self.lakefs_commit,
                        **meta,
                    }

                    batch.add_data_object(
                        data_object=properties,
                        class_name=self.class_name,
                        vector=embedding,
                        uuid=doc_id,
                    )

            return {"added_count": len(embeddings)}

        finally:
            if span:
                span.end()

    def query(
        self,
        query_embedding: List[float],
        top_k: int = 5,
        where_filter: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Query the class, filtered to this store's lakeFS commit."""
        span = self._tracer.start_span("rag.vector_store.query") if self._tracer else None

        try:
            if span:
                span.set_attribute("rag.vector_store.type", "weaviate")
                span.set_attribute("rag.vector_store.query_top_k", top_k)

            version_filter: Dict[str, Any] = {
                "path": ["lakefs_commit"],
                "operator": "Equal",
                "valueText": self.lakefs_commit,
            }

            if where_filter:
                combined_filter: Dict[str, Any] = {
                    "operator": "And",
                    "operands": [version_filter, where_filter],
                }
            else:
                combined_filter = version_filter

            result = (
                self.client.query
                .get(self.class_name, ["document_id", "text", "lakefs_commit", "lakefs_repository"])
                .with_near_vector({"vector": query_embedding})
                .with_limit(top_k)
                .with_where(combined_filter)
                .with_additional(["distance", "id"])
                .do()
            )

            objects = result.get("data", {}).get("Get", {}).get(self.class_name, [])

            if span:
                span.set_attribute(RAG_RETRIEVED_COUNT, len(objects))

            return [
                {
                    "id": obj.get("_additional", {}).get("id"),
                    "distance": obj.get("_additional", {}).get("distance"),
                    "text": obj.get("text"),
                    "metadata": {
                        "document_id": obj.get("document_id"),
                        "lakefs_commit": obj.get("lakefs_commit"),
                        "lakefs_repository": obj.get("lakefs_repository"),
                    },
                }
                for obj in objects
            ]

        finally:
            if span:
                span.end()

    def delete_by_id(self, document_id: str) -> bool:
        """Delete an object by ID. Returns False when the delete fails."""
        try:
            self.client.data_object.delete(document_id, class_name=self.class_name)
            return True
        except Exception as e:
            logger.error(f"Failed to delete {document_id}: {e}")
            return False

    def get_schema(self) -> Dict[str, Any]:
        """Return the class schema, or an empty dict when the lookup fails."""
        try:
            return self.client.schema.get(self.class_name)
        except Exception as e:
            logger.error(f"Failed to get schema: {e}")
            return {}
