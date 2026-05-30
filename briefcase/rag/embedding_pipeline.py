"""
Versioned embedding pipeline with atomic manifests.

An EmbeddingManifest captures the complete state of an embedding index:
  - Which documents were embedded (IDs + content hashes)
  - Which model + version produced the embeddings
  - The lakeFS commit the documents came from
  - Whether the index is stale (documents or model changed)
"""

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from briefcase._logging import get_logger

import briefcase.semantic_conventions.rag as rag_conventions  # noqa: F401

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Document:
    """Document for embedding."""
    id: str
    content: str
    metadata: dict = field(default_factory=dict)
    path: str = ""  # lakeFS path

    @property
    def content_hash(self) -> str:
        """SHA-256 of document content."""
        return hashlib.sha256(self.content.encode()).hexdigest()


@dataclass
class EmbeddingRecord:
    """Single document's embedding with provenance."""
    document_id: str
    document_hash: str  # SHA-256 of content at embed time
    embedding: List[float]
    model: str
    model_version: str
    created_at: str  # ISO-8601


@dataclass
class EmbeddingBatch:
    """Batch of embeddings."""
    batch_id: str
    model: str
    model_version: str
    dimensions: int
    embeddings: List[List[float]]
    document_ids: List[str]
    document_hashes: List[str]
    created_at: datetime
    source_commit: str

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        # Don't serialize full embeddings to JSON (too large)
        d.pop("embeddings", None)
        return d


class ManifestStatus(Enum):
    """Status of an embedding manifest."""
    CURRENT = "current"
    STALE_DOCUMENTS = "stale_documents"
    STALE_MODEL = "stale_model"
    STALE_BOTH = "stale_both"
    REBUILDING = "rebuilding"


@dataclass
class EmbeddingManifest:
    """
    Atomic manifest capturing the complete state of an embedding index.

    This is the core versioning artifact: it records exactly which documents
    were embedded, with which model, at which lakeFS commit.
    """
    manifest_id: str
    index_name: str
    model: str
    model_version: str
    dimensions: int
    source_commit: str  # lakeFS commit SHA documents came from
    document_count: int
    document_hashes: Dict[str, str]  # doc_id -> content_hash at embed time
    batch_ids: List[str]
    created_at: str  # ISO-8601
    status: str = ManifestStatus.CURRENT.value
    parent_manifest_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EmbeddingManifest":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @classmethod
    def from_json(cls, s: str) -> "EmbeddingManifest":
        return cls.from_dict(json.loads(s))

    @property
    def manifest_hash(self) -> str:
        """Deterministic hash of manifest content (for integrity checking)."""
        content = json.dumps({
            "index_name": self.index_name,
            "model": self.model,
            "model_version": self.model_version,
            "source_commit": self.source_commit,
            "document_hashes": self.document_hashes,
        }, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()


@dataclass
class InvalidationReport:
    """Report describing why an embedding index is stale."""
    manifest_id: str
    index_name: str
    is_valid: bool
    status: str  # ManifestStatus value
    added_documents: List[str] = field(default_factory=list)
    removed_documents: List[str] = field(default_factory=list)
    changed_documents: List[str] = field(default_factory=list)
    model_changed: bool = False
    old_model: Optional[str] = None
    new_model: Optional[str] = None
    old_model_version: Optional[str] = None
    new_model_version: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    # lakeFS diff fields (populated when branch_manager is passed to check_invalidation)
    diff_entries: Optional[List] = None   # list[DiffEntry] from branches.py
    source_ref: Optional[str] = None      # ref compared from
    target_ref: Optional[str] = None      # ref compared to

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class VersionedEmbeddingPipeline:
    """
    Pipeline that creates embeddings from documents and stores them
    with atomic manifests for full version tracking.

    Usage:
        pipeline = VersionedEmbeddingPipeline(embedding_model=model)
        batch = pipeline.create_embedding_batch(documents)
        manifest = pipeline.create_manifest("my_index", [batch])
        report = pipeline.check_invalidation("my_index", current_docs)
    """

    def __init__(
        self,
        embedding_model: Any = None,
        lakefs_client: Any = None,
        repository: Optional[str] = None,
        branch: str = "main",
    ):
        self.model = embedding_model
        self.lakefs = lakefs_client
        self.repository = repository
        self.branch = branch

        # index_name -> list of EmbeddingManifest (most recent last)
        self._manifests: Dict[str, List[EmbeddingManifest]] = {}

        # batch_id -> EmbeddingBatch
        self._batches: Dict[str, EmbeddingBatch] = {}

    # ------------------------------------------------------------------
    # Embedding creation
    # ------------------------------------------------------------------

    def create_embedding_batch(
        self,
        documents: List[Document],
        batch_id: Optional[str] = None,
        source_commit: Optional[str] = None,
    ) -> EmbeddingBatch:
        """
        Create embeddings for a batch of documents.

        If self.model has an `embed(texts)` method, uses it.
        Otherwise falls back to mock embeddings.
        """
        if batch_id is None:
            batch_id = f"batch_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

        if source_commit is None:
            if self.lakefs and hasattr(self.lakefs, 'get_commit'):
                try:
                    source_commit = self.lakefs.get_commit()
                except Exception:
                    source_commit = "unknown"
            else:
                source_commit = "unknown"

        model_name = getattr(self.model, 'name', 'mock-model') if self.model else 'mock-model'
        model_version = getattr(self.model, 'version', '1.0') if self.model else '1.0'

        # Generate embeddings
        texts = [doc.content for doc in documents]
        if self.model and hasattr(self.model, 'embed'):
            try:
                raw_embeddings = self.model.embed(texts)
                embeddings = [list(e) for e in raw_embeddings]
            except Exception as e:
                logger.warning(f"Embedding model failed, using mock: {e}")
                embeddings = self._mock_embeddings(len(texts))
        else:
            embeddings = self._mock_embeddings(len(texts))

        dimensions = len(embeddings[0]) if embeddings else 0
        document_ids = [doc.id for doc in documents]
        document_hashes = [doc.content_hash for doc in documents]

        batch = EmbeddingBatch(
            batch_id=batch_id,
            model=model_name,
            model_version=model_version,
            dimensions=dimensions,
            embeddings=embeddings,
            document_ids=document_ids,
            document_hashes=document_hashes,
            created_at=datetime.utcnow(),
            source_commit=source_commit,
        )

        self._batches[batch_id] = batch
        logger.info(f"Created embedding batch {batch_id}: {len(documents)} docs, {dimensions}d")

        return batch

    # ------------------------------------------------------------------
    # Manifest management
    # ------------------------------------------------------------------

    def create_manifest(
        self,
        index_name: str,
        batches: List[EmbeddingBatch],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> EmbeddingManifest:
        """
        Create an atomic manifest from one or more embedding batches.

        The manifest captures the full state of the index at this point.
        """
        if not batches:
            raise ValueError("At least one batch is required to create a manifest")

        # Aggregate document hashes across batches
        doc_hashes: Dict[str, str] = {}
        batch_ids = []
        total_docs = 0

        for batch in batches:
            batch_ids.append(batch.batch_id)
            for doc_id, doc_hash in zip(batch.document_ids, batch.document_hashes):
                doc_hashes[doc_id] = doc_hash
                total_docs += 1

        # Use first batch for model info (all batches should use same model)
        first = batches[0]
        source_commit = first.source_commit

        parent = self.get_latest_manifest(index_name)
        manifest_id = f"{index_name}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{first.source_commit[:8]}"

        manifest = EmbeddingManifest(
            manifest_id=manifest_id,
            index_name=index_name,
            model=first.model,
            model_version=first.model_version,
            dimensions=first.dimensions,
            source_commit=source_commit,
            document_count=len(doc_hashes),
            document_hashes=doc_hashes,
            batch_ids=batch_ids,
            created_at=datetime.utcnow().isoformat(),
            status=ManifestStatus.CURRENT.value,
            parent_manifest_id=parent.manifest_id if parent else None,
            metadata=metadata or {},
        )

        self._manifests.setdefault(index_name, []).append(manifest)

        # Store to lakeFS if available
        if self.lakefs and self.repository:
            path = f"manifests/{index_name}/{manifest_id}.json"
            try:
                self.lakefs.upload_object(
                    self.repository, self.branch, path, manifest.to_json()
                )
            except Exception as e:
                logger.warning(f"Failed to upload manifest to lakeFS: {e}")

        logger.info(
            f"Created manifest {manifest_id}: {len(doc_hashes)} docs, "
            f"model={first.model}@{first.model_version}"
        )

        return manifest

    def get_latest_manifest(self, index_name: str) -> Optional[EmbeddingManifest]:
        """Get the most recent manifest for an index."""
        manifests = self._manifests.get(index_name, [])
        return manifests[-1] if manifests else None

    def get_manifests(
        self,
        index_name: str,
        limit: Optional[int] = None,
    ) -> List[EmbeddingManifest]:
        """Get manifests for an index, optionally limited."""
        manifests = self._manifests.get(index_name, [])
        if limit:
            manifests = manifests[-limit:]
        return manifests

    def get_all_index_names(self) -> List[str]:
        """List all tracked index names."""
        return list(self._manifests.keys())

    # ------------------------------------------------------------------
    # Invalidation detection
    # ------------------------------------------------------------------

    def check_invalidation(
        self,
        index_name: str,
        current_documents: List[Document],
        current_model: Optional[str] = None,
        current_model_version: Optional[str] = None,
        branch_manager=None,
        source_commit: Optional[str] = None,
    ) -> InvalidationReport:
        """
        Check whether an embedding index is stale.

        Compares the latest manifest against current documents and model.
        Returns an InvalidationReport describing what changed.

        When *branch_manager* and *source_commit* are provided the report is
        enriched with lakeFS diff data: ``diff_entries``, ``source_ref``, and
        ``target_ref`` are populated by calling
        ``branch_manager.diff(source_commit, manifest.source_commit)``.
        Existing hash-only comparison behaviour is unchanged when
        *branch_manager* is ``None``.

        Args:
            index_name: Name of the embedding index to check.
            current_documents: Current document set to compare against the
                manifest.
            current_model: Optional model name override.
            current_model_version: Optional model version override.
            branch_manager: Optional branch manager instance
                used to fetch lakeFS diff data (requires enterprise package).
            source_commit: The "before" lakeFS commit ref to diff from.
                Required when *branch_manager* is provided.

        Returns:
            :class:`InvalidationReport` describing the staleness state.
        """
        manifest = self.get_latest_manifest(index_name)

        if manifest is None:
            # No manifest  nothing to invalidate, but also nothing to validate
            return InvalidationReport(
                manifest_id="none",
                index_name=index_name,
                is_valid=False,
                status=ManifestStatus.STALE_DOCUMENTS.value,
                added_documents=[doc.id for doc in current_documents],
            )

        # Build current doc hash map
        current_hashes = {doc.id: doc.content_hash for doc in current_documents}
        manifest_hashes = manifest.document_hashes

        # Detect document changes
        current_ids = set(current_hashes.keys())
        manifest_ids = set(manifest_hashes.keys())

        added = sorted(current_ids - manifest_ids)
        removed = sorted(manifest_ids - current_ids)
        changed = sorted([
            doc_id for doc_id in current_ids & manifest_ids
            if current_hashes[doc_id] != manifest_hashes[doc_id]
        ])

        docs_changed = bool(added or removed or changed)

        # Detect model changes
        model_changed = False
        effective_model = current_model or (
            getattr(self.model, 'name', None) if self.model else None
        )
        effective_version = current_model_version or (
            getattr(self.model, 'version', None) if self.model else None
        )

        if effective_model and effective_model != manifest.model:
            model_changed = True
        if effective_version and effective_version != manifest.model_version:
            model_changed = True

        # Determine status
        if docs_changed and model_changed:
            status = ManifestStatus.STALE_BOTH
        elif docs_changed:
            status = ManifestStatus.STALE_DOCUMENTS
        elif model_changed:
            status = ManifestStatus.STALE_MODEL
        else:
            status = ManifestStatus.CURRENT

        is_valid = status == ManifestStatus.CURRENT

        # Update manifest status
        if not is_valid:
            manifest.status = status.value

        report = InvalidationReport(
            manifest_id=manifest.manifest_id,
            index_name=index_name,
            is_valid=is_valid,
            status=status.value,
            added_documents=added,
            removed_documents=removed,
            changed_documents=changed,
            model_changed=model_changed,
            old_model=manifest.model if model_changed else None,
            new_model=effective_model if model_changed else None,
            old_model_version=manifest.model_version if model_changed else None,
            new_model_version=effective_version if model_changed else None,
        )

        # Enrich with lakeFS diff data when a BranchManager is supplied.
        if branch_manager is not None and source_commit is not None:
            target_ref = manifest.source_commit
            try:
                report.diff_entries = branch_manager.diff(source_commit, target_ref)
                report.source_ref = source_commit
                report.target_ref = target_ref
            except Exception as exc:
                logger.warning("Failed to get lakeFS diff for invalidation report: %s", exc)

        return report

    def rebuild_index(
        self,
        index_name: str,
        documents: List[Document],
        source_commit: Optional[str] = None,
        batch_id: Optional[str] = None,
    ) -> EmbeddingManifest:
        """
        Rebuild an embedding index: create new embeddings and a new manifest.

        Convenience method that chains create_embedding_batch + create_manifest.
        """
        batch = self.create_embedding_batch(
            documents, batch_id=batch_id, source_commit=source_commit
        )
        manifest = self.create_manifest(index_name, [batch])
        return manifest

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _mock_embeddings(count: int, dimensions: int = 128) -> List[List[float]]:
        """Create mock embeddings for testing."""
        return [[0.0] * dimensions for _ in range(count)]
