"""
Semantic conventions for RAG versioning.
"""

# Document versioning
RAG_DOCUMENT_ID = "rag.document.id"
RAG_DOCUMENT_VERSION = "rag.document.version"  # lakeFS commit SHA
RAG_DOCUMENT_PATH = "rag.document.path"  # lakeFS path
RAG_DOCUMENT_HASH = "rag.document.hash"  # Content hash (SHA-256)
RAG_DOCUMENT_UPDATED_AT = "rag.document.updated_at"  # ISO 8601 timestamp

# Embedding versioning
RAG_EMBEDDING_MODEL = "rag.embedding.model"  # e.g., "text-embedding-3-large"
RAG_EMBEDDING_MODEL_VERSION = "rag.embedding.model.version"  # e.g., "v3"
RAG_EMBEDDING_DIMENSIONS = "rag.embedding.dimensions"  # e.g., 3072
RAG_EMBEDDING_CREATED_AT = "rag.embedding.created_at"
RAG_EMBEDDING_BATCH_ID = "rag.embedding.batch_id"  # ETL job ID

# Vector index versioning
RAG_INDEX_NAME = "rag.index.name"
RAG_INDEX_VERSION = "rag.index.version"  # lakeFS commit SHA
RAG_INDEX_SIZE = "rag.index.size"  # Number of vectors
RAG_INDEX_UPDATED_AT = "rag.index.updated_at"

# Retrieval provenance
RAG_QUERY_TEXT = "rag.query.text"
RAG_QUERY_EMBEDDING_MODEL = "rag.query.embedding.model"
RAG_TOP_K = "rag.retrieval.top_k"
RAG_SIMILARITY_THRESHOLD = "rag.retrieval.similarity_threshold"
RAG_RETRIEVED_COUNT = "rag.retrieval.count"

# Retrieved document tracking (for each result)
RAG_RESULT_DOCUMENT_ID = "rag.result.document.id"
RAG_RESULT_SCORE = "rag.result.score"
RAG_RESULT_RANK = "rag.result.rank"
RAG_RESULT_DOCUMENT_VERSION = "rag.result.document.version"  # SHA when retrieved

# Version drift detection
RAG_VERSION_DRIFT = "rag.version.drift"  # boolean
RAG_VERSION_DRIFT_REASON = "rag.version.drift.reason"
