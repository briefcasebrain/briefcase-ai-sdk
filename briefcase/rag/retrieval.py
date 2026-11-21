"""
Instrumented retrieval that captures version provenance.

:class:`InstrumentedRetriever` wraps a caller-supplied retriever or vector
store, delegates the search to it, and stamps every result with the lakeFS
commit the documents were read at. It never invents results: with nothing
to delegate to, :meth:`InstrumentedRetriever.retrieve` raises.
"""

from typing import Any, Callable, List, Optional
from dataclasses import dataclass
from briefcase._logging import get_logger

from briefcase.semantic_conventions.rag import *  # noqa: F401,F403

logger = get_logger(__name__)

UNKNOWN_VERSION = "unknown"


@dataclass
class RetrievalResult:
    """Result from retrieval operation."""
    document_id: str
    content: str
    score: float
    rank: int
    document_version: str  # lakeFS commit SHA, or "unknown" if unresolved
    metadata: dict


class InstrumentedRetriever:
    """
    Retriever wrapper that captures version provenance.

    The search itself is delegated to ``vector_store``, which may be:

    - a callable ``(query, top_k) -> hits``;
    - an object with ``retrieve(query, top_k)`` or ``search(query, top_k)``;
    - a LangChain-style store with
      ``similarity_search_with_score(query, k=top_k)``.

    Each hit may be a :class:`RetrievalResult`, a mapping (keys
    ``id``/``document_id``, ``content``/``text``/``page_content``, ``score``,
    ``metadata``), a ``(document, score)`` pair, or a document object with
    ``page_content`` / ``metadata`` attributes.

    ``document_version`` on each result is the commit resolved from
    ``lakefs_client.get_commit()`` (a per-hit ``document_version`` or
    ``metadata["commit"]`` wins); it is ``"unknown"`` when unresolved, never
    a made-up id.
    """

    def __init__(
        self,
        vector_store: Any,
        lakefs_client: Any = None,
        repository: str = "",
        branch: str = "main"
    ):
        self.vector_store = vector_store
        self.lakefs = lakefs_client
        self.repository = repository
        self.branch = branch

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        similarity_threshold: float = 0.7
    ) -> List[RetrievalResult]:
        """
        Retrieve documents from the wrapped store and capture version provenance.

        Hits scoring below ``similarity_threshold`` are dropped. Errors from
        the wrapped store propagate.

        Raises:
            RuntimeError: No retriever/vector store is configured.
            TypeError: The configured store exposes no supported search method.
        """
        search = self._resolve_search()
        hits = search(query, top_k)
        commit = self._resolve_commit()

        results: List[RetrievalResult] = []
        for hit in list(hits or [])[:top_k]:
            result = self._to_result(hit, len(results), commit)
            if result.score < similarity_threshold:
                continue
            result.rank = len(results)
            results.append(result)

        logger.info(f"Retrieved {len(results)} documents for query: {query[:50]}...")

        return results

    # ------------------------------------------------------------------

    def _resolve_search(self) -> Callable[[str, int], Any]:
        store = self.vector_store
        if store is None:
            raise RuntimeError(
                "InstrumentedRetriever has no vector_store to delegate to; "
                "pass a retriever, vector store, or callable(query, top_k)."
            )
        for name in ("retrieve", "search"):
            method = getattr(store, name, None)
            if callable(method):
                return method
        method = getattr(store, "similarity_search_with_score", None)
        if callable(method):
            return lambda query, top_k: method(query, k=top_k)
        if callable(store):
            return store
        raise TypeError(
            f"vector_store {type(store).__name__!r} has no retrieve/search/"
            "similarity_search_with_score method and is not callable"
        )

    def _resolve_commit(self) -> str:
        get_commit = getattr(self.lakefs, "get_commit", None)
        if not callable(get_commit):
            return UNKNOWN_VERSION
        try:
            return str(get_commit())
        except Exception as e:
            logger.warning("Could not resolve lakeFS commit for retrieval: %s", e)
            return UNKNOWN_VERSION

    @staticmethod
    def _to_result(hit: Any, rank: int, commit: str) -> RetrievalResult:
        if isinstance(hit, RetrievalResult):
            if not hit.document_version:
                hit.document_version = commit
            return hit

        score: Optional[float] = None
        if isinstance(hit, tuple) and len(hit) == 2:
            hit, score = hit

        if isinstance(hit, dict):
            data = hit
        else:
            data = {
                "id": getattr(hit, "id", None),
                "content": getattr(hit, "page_content", getattr(hit, "content", None)),
                "score": getattr(hit, "score", None),
                "metadata": getattr(hit, "metadata", None),
                "document_version": getattr(hit, "document_version", None),
            }

        metadata = dict(data.get("metadata") or {})
        document_id = data.get("document_id", data.get("id"))
        if document_id is None:
            document_id = metadata.get("id", metadata.get("document_id"))
        if document_id is None:
            raise ValueError(f"retrieval hit at position {rank} has no document id")
        content = data.get("content")
        if content is None:
            content = data.get("text", data.get("page_content"))
        if score is None:
            score = data.get("score")
        if score is None:
            raise ValueError(f"retrieval hit {document_id!r} has no score")

        return RetrievalResult(
            document_id=str(document_id),
            content="" if content is None else str(content),
            score=float(score),
            rank=rank,
            document_version=str(
                data.get("document_version") or metadata.get("commit") or commit
            ),
            metadata=metadata,
        )
