"""RAG (Retrieval-Augmented Generation) versioning components.

Pure-Python; no third-party dependencies. The ``rag`` extra is a metadata
alias kept for backwards compatibility and installs nothing extra.
"""

from briefcase.rag.embedding_pipeline import (
    VersionedEmbeddingPipeline,
    Document,
    EmbeddingBatch,
    EmbeddingManifest,
    EmbeddingRecord,
    ManifestStatus,
    InvalidationReport,
)
from briefcase.rag.retrieval import InstrumentedRetriever, RetrievalResult

__all__ = [
    "VersionedEmbeddingPipeline",
    "Document",
    "EmbeddingBatch",
    "EmbeddingManifest",
    "EmbeddingRecord",
    "ManifestStatus",
    "InvalidationReport",
    "InstrumentedRetriever",
    "RetrievalResult",
]
