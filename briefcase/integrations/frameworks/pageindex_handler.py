"""PageIndex tree-aware tracer for Briefcase.

Wraps PageIndexClient.chat_completions() to emit decision records with
pageindex.tree.* attributes. PageIndex performs tree traversal server-side
and does not expose per-query traversal details, so the tracer calls
get_tree() after each retrieval (best effort) and computes:

  - pageindex.tree.depth: maximum depth of the document tree
  - pageindex.tree.nodes_visited: total node count (an upper-bound proxy)
  - pageindex.tree.path: readable summary "root > Section 1 > ... (N more)"
  - pageindex.tree.backtrack_count: always 0 (not available from the API)

The tracer degrades gracefully: it can be instantiated without pageindex
installed, and raises ImportError only when a wrapped method is called with
no client available.

Usage:
    from briefcase.integrations.frameworks import PageIndexTracer

    tracer = PageIndexTracer(api_key="...", context_version="v2")
    response = tracer.chat_completions(
        messages=[{"role": "user", "content": "What is the capital?"}],
        doc_id="pi-abc123",
    )
    records = tracer.get_records()
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from briefcase._export_mixin import ExportMixin

# Optional dependency guard: used only to build a client from api_key.
try:
    from pageindex import PageIndexClient as _PageIndexClient
    _PAGEINDEX_AVAILABLE = True
except ImportError:
    _PageIndexClient = None  # type: ignore[assignment]
    _PAGEINDEX_AVAILABLE = False

_INSTALL_HINT = (
    "pageindex is required for this operation. "
    "Install with: pip install pageindex  or  pip install briefcase-ai[pageindex]"
)


# Tree computation helpers

def _compute_tree_depth(node: Dict[str, Any], current_depth: int = 0) -> int:
    """Recursively compute the maximum depth of a tree node. Children live under 'nodes'."""
    children = node.get("nodes", [])
    if not children:
        return current_depth
    return max(_compute_tree_depth(child, current_depth + 1) for child in children)


def _count_tree_nodes(node: Dict[str, Any]) -> int:
    """Recursively count all nodes in the tree, root inclusive."""
    children = node.get("nodes", [])
    return 1 + sum(_count_tree_nodes(child) for child in children)


def _build_tree_path(node: Dict[str, Any], max_sections: int = 3) -> str:
    """Build a readable traversal path summary from the tree root."""
    parts: List[str] = []
    root_title = node.get("title") or node.get("node_id") or "root"
    parts.append(str(root_title))

    children = node.get("nodes", [])
    shown = children[:max_sections]
    for child in shown:
        child_title = child.get("title") or child.get("node_id") or "node"
        parts.append(str(child_title))

    if len(children) > max_sections:
        parts.append(f"... ({len(children) - max_sections} more)")

    return " > ".join(parts)


class PageIndexTracer(ExportMixin):
    """Wrapper around PageIndexClient that captures tree-aware decision records.

    Intercepts chat_completions() to:
      1. Record doc_id, messages, and timing
      2. Call get_tree() to compute tree depth and node count (best effort)
      3. Emit a decision record with pageindex.* attributes
      4. Export via the configured exporter (async by default)

    The underlying PageIndexClient is either supplied directly (``client``
    parameter) or created from ``api_key``. When neither is available,
    instantiation still succeeds; ImportError is raised only when a wrapped
    method is actually called.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        context_version: Optional[str] = None,
        async_capture: bool = True,
        client: Optional[Any] = None,
        fetch_tree_metadata: bool = True,
        exporter: Any = None,
    ) -> None:
        """
        Args:
            api_key: PageIndex API key. Used when ``client`` is not provided.
            context_version: Optional version tag added to all decision records.
            async_capture: If True (default), export runs in the background.
            client: Existing PageIndexClient instance (overrides api_key).
            fetch_tree_metadata: If True (default), calls get_tree() after each
                chat_completions() to enrich the record with tree structure.
            exporter: Briefcase exporter instance; falls back to the global config.
        """
        self.api_key = api_key
        self.context_version = context_version
        self.async_capture = async_capture
        self.fetch_tree_metadata = fetch_tree_metadata
        self._exporter = exporter

        if client is not None:
            self._client: Optional[Any] = client
        elif _PAGEINDEX_AVAILABLE and api_key is not None:
            self._client = _PageIndexClient(api_key=api_key)
        else:
            self._client = None

        self._records: List[Dict[str, Any]] = []

    # Public API

    def get_records(self) -> List[Dict[str, Any]]:
        """Return all captured decision records."""
        return list(self._records)

    def clear(self) -> None:
        """Clear all captured decision records."""
        self._records.clear()

    def chat_completions(
        self,
        messages: List[Dict[str, str]],
        doc_id: Optional[Union[str, List[str]]] = None,
        **kwargs: Any,
    ) -> Any:
        """Wrapped PageIndexClient.chat_completions() with decision capture.

        Calls the underlying client, then get_tree() (when configured) to
        attach tree metadata. On API error, records an error decision and
        re-raises. Returns the original PageIndex response unchanged.
        """
        if self._client is None:
            raise ImportError(_INSTALL_HINT)

        started_at = datetime.now(timezone.utc)
        decision_id = str(uuid.uuid4())

        # Normalize doc_id to a single string for the record
        primary_doc_id = _normalize_doc_id(doc_id)

        try:
            response = self._client.chat_completions(
                messages=messages, doc_id=doc_id, **kwargs
            )
        except Exception as exc:
            ended_at = datetime.now(timezone.utc)
            record = self._build_record(
                decision_id=decision_id,
                doc_id=primary_doc_id,
                messages=messages,
                response=None,
                started_at=started_at,
                ended_at=ended_at,
                error=str(exc),
                tree_meta=None,
            )
            self._records.append(record)
            self._trigger_export(record)
            raise

        ended_at = datetime.now(timezone.utc)

        tree_meta = self._fetch_tree_meta(primary_doc_id)
        record = self._build_record(
            decision_id=decision_id,
            doc_id=primary_doc_id,
            messages=messages,
            response=response,
            started_at=started_at,
            ended_at=ended_at,
            tree_meta=tree_meta,
        )
        self._records.append(record)
        self._trigger_export(record)

        return response

    def get_tree(self, doc_id: str, **kwargs: Any) -> Any:
        """Wrapped get_tree(); delegates directly to the underlying client."""
        if self._client is None:
            raise ImportError(_INSTALL_HINT)
        return self._client.get_tree(doc_id, **kwargs)

    # Internal helpers

    def _fetch_tree_meta(self, doc_id: str) -> Dict[str, Any]:
        """Call get_tree() and compute tree metadata. Returns {} on any failure."""
        if not self.fetch_tree_metadata or not doc_id or self._client is None:
            return {}
        try:
            tree_response = self._client.get_tree(doc_id)
            # Support both {"tree": {...}} and {"data": {...}} response shapes
            tree = tree_response.get("tree") or tree_response.get("data")
            if not isinstance(tree, dict):
                return {}

            return {
                "pageindex.tree.depth": _compute_tree_depth(tree),
                "pageindex.tree.nodes_visited": _count_tree_nodes(tree),
                "pageindex.tree.path": _build_tree_path(tree),
                "pageindex.tree.backtrack_count": 0,
            }
        except Exception:
            return {}

    def _build_record(
        self,
        decision_id: str,
        doc_id: str,
        messages: List[Dict[str, str]],
        response: Any,
        started_at: datetime,
        ended_at: datetime,
        error: Optional[str] = None,
        tree_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Assemble the decision record dict."""
        delta_ms = (ended_at - started_at).total_seconds() * 1000

        record: Dict[str, Any] = {
            "decision_id": decision_id,
            "decision_type": "pageindex_retrieval",
            "function_name": "PageIndexTracer.chat_completions",
            "inputs": {"messages": messages, "doc_id": doc_id},
            "outputs": {},
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "execution_time_ms": delta_ms,
            # pageindex-specific attributes (always present)
            "pageindex.doc_id": doc_id,
            "pageindex.retrieval_method": "tree_search",
        }

        if self.context_version is not None:
            record["context_version"] = self.context_version

        if error:
            record["error"] = error

        if response is not None and isinstance(response, dict):
            choices = response.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")
                record["outputs"] = {"content": content}

        # Apply tree metadata or set defaults so the keys always exist
        if tree_meta:
            record.update(tree_meta)
        else:
            record.setdefault("pageindex.tree.depth", 0)
            record.setdefault("pageindex.tree.nodes_visited", 0)
            record.setdefault("pageindex.tree.path", "")
            record.setdefault("pageindex.tree.backtrack_count", 0)

        return record


# Module-level helpers

def _normalize_doc_id(doc_id: Optional[Union[str, List[str]]]) -> str:
    """Normalize doc_id to a single string for storage in the record."""
    if isinstance(doc_id, list):
        return doc_id[0] if doc_id else ""
    return str(doc_id) if doc_id is not None else ""


def require_pageindex() -> None:
    """Raise ImportError with an install hint when pageindex is absent."""
    if not _PAGEINDEX_AVAILABLE:
        raise ImportError(_INSTALL_HINT)
