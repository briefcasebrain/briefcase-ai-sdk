"""
Instrumented retrieval that captures version provenance.

.. warning::
   :class:`InstrumentedRetriever` is a **reference implementation**. Its
   :meth:`~InstrumentedRetriever.retrieve` returns placeholder results so the
   provenance-capture shape can be demonstrated end to end; it performs no real
   vector search. Subclass it and override ``retrieve`` to wire a real vector
   store and lakeFS commit resolution.
"""

from typing import List, Optional
from dataclasses import dataclass
from briefcase._logging import get_logger
import warnings

from briefcase.semantic_conventions.rag import *

logger = get_logger(__name__)


@dataclass
class RetrievalResult:
    """Result from retrieval operation."""
    document_id: str
    content: str
    score: float
    rank: int
    document_version: str  # lakeFS commit SHA
    metadata: dict


class InstrumentedRetriever:
    """
    Reference retriever that captures full version provenance.

    NOTE: this base implementation returns placeholder results (see the module
    docstring). Override :meth:`retrieve` with a real vector-store query to use
    it in production.
    """

    def __init__(
        self,
        vector_store,
        lakefs_client,
        repository: str,
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
        Retrieve documents and capture version provenance.

        Reference implementation: returns placeholder results and emits a
        :class:`RuntimeWarning`. Override in a subclass for real retrieval.
        """
        warnings.warn(
            "InstrumentedRetriever.retrieve() is a reference stub that returns "
            "placeholder results; override it with a real vector-store query.",
            RuntimeWarning,
            stacklevel=2,
        )
        results = []

        for i in range(min(top_k, 3)):  # placeholder: return up to 3 stub results
            result = RetrievalResult(
                document_id=f"doc_{i}",
                content=f"Mock document content for query: {query}",
                score=0.95 - (i * 0.05),
                rank=i,
                document_version="mock_commit_sha",
                metadata={"source": "mock"}
            )
            results.append(result)

        logger.info(f"Retrieved {len(results)} documents for query: {query[:50]}...")

        return results
