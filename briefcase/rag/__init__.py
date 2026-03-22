"""RAG (Retrieval-Augmented Generation) versioning components."""

try:
    import pyarrow as _pyarrow  # type: ignore
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
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "briefcase.rag requires the 'rag' extra.\n"
        "Install it with: pip install briefcase-ai[rag]"
    ) from exc
else:
    _ = _pyarrow

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
