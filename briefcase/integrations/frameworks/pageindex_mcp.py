"""PageIndex MCP response observer for Briefcase.

Post-processes decision records produced by other handlers when PageIndex
is reached through MCP (Model Context Protocol) tools. In that path the
call surfaces as an ordinary tool invocation whose output is a JSON-encoded
string; this observer detects those records and enriches them in place with
pageindex.* attributes.

Detection (applied in order):
  - Tool/function name contains any of: "pageindex", "page_index",
    "pi_search", "pi_chat", "pi_retrieve" (case-insensitive)
  - OR the output JSON contains "doc_id" or "retrieval_id" keys
  - OR the output JSON contains a "nodes" array or a "tree" dict

This component never imports pageindex; it only parses JSON.

Usage:
    from briefcase.integrations.frameworks import PageIndexMCPObserver

    observer = PageIndexMCPObserver()

    # Enrich a single record in place
    enriched = observer.observe(record)  # True when enriched

    # Post-process all records from a handler
    for record in handler.get_decisions_as_dicts():
        observer.observe(record)
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

# Keywords that identify a tool as a PageIndex MCP tool (case-insensitive)
_PAGEINDEX_TOOL_KEYWORDS = (
    "pageindex",
    "page_index",
    "pi_search",
    "pi_chat",
    "pi_retrieve",
)

# Keys whose presence in a JSON response indicates a PageIndex server response
_PAGEINDEX_RESPONSE_KEYS = ("doc_id", "retrieval_id")


class PageIndexMCPObserver:
    """Detects PageIndex MCP tool results in decision records and enriches
    them with pageindex.* tree metadata attributes.

    Operates purely on the JSON string content of MCP tool responses; the
    pageindex package is never required.

    Attributes added to matching records (in place):
        pageindex.doc_id               (str)
        pageindex.retrieval_method     (str)  always "tree_search"
        pageindex.tree.depth           (int)
        pageindex.tree.nodes_visited   (int)
        pageindex.tree.path            (str)
        pageindex.tree.backtrack_count (int)  always 0
    """

    def __init__(self) -> None:
        self._observed_count: int = 0
        self._enriched_count: int = 0

    # Public API

    def observe(self, record: Dict[str, Any]) -> bool:
        """Inspect a decision record and enrich it with pageindex.* attributes
        when it appears to be a PageIndex MCP tool call.

        Args:
            record: Decision record dict (mutated in place when detected).

        Returns:
            True when the record was identified as a PageIndex MCP call and
            enriched.
        """
        try:
            self._observed_count += 1
            if not self._is_pageindex_record(record):
                return False
            self._enrich_record(record)
            self._enriched_count += 1
            return True
        except Exception:
            return False

    def is_pageindex_mcp_response(self, record: Dict[str, Any]) -> bool:
        """Check whether a decision record looks like a PageIndex MCP tool
        call, without modifying the record."""
        try:
            return self._is_pageindex_record(record)
        except Exception:
            return False

    @property
    def observed_count(self) -> int:
        """Total number of records passed to observe()."""
        return self._observed_count

    @property
    def enriched_count(self) -> int:
        """Total number of records that were identified and enriched."""
        return self._enriched_count

    # Internal detection

    def _is_pageindex_record(self, record: Dict[str, Any]) -> bool:
        """Return True when the record appears to originate from a PageIndex
        MCP tool call. Checks the tool name first, then output content."""
        # 1. Name-based detection (cheapest check first)
        name = (
            record.get("function_name", "")
            or record.get("tool_name", "")
            or record.get("name", "")
        ).lower()

        if any(kw in name for kw in _PAGEINDEX_TOOL_KEYWORDS):
            return True

        # 2. Content-based detection: parse the output as JSON
        output_str = _extract_output_str(record)
        if not output_str:
            return False

        parsed = _try_parse_json(output_str)
        if not isinstance(parsed, dict):
            return False

        # doc_id or retrieval_id marks a PageIndex response
        if any(key in parsed for key in _PAGEINDEX_RESPONSE_KEYS):
            return True

        # Flat tree at the root level ('nodes' list present)
        if "nodes" in parsed and isinstance(parsed.get("nodes"), list):
            return True

        # Nested tree under a 'tree' key
        if "tree" in parsed and isinstance(parsed.get("tree"), dict):
            return True

        return False

    def _enrich_record(self, record: Dict[str, Any]) -> None:
        """Add pageindex.* attributes to the record in place."""
        output_str = _extract_output_str(record)
        parsed: Optional[Dict[str, Any]] = None
        if output_str:
            parsed = _try_parse_json(output_str)
            if not isinstance(parsed, dict):
                parsed = None

        # Extract doc_id from the output, then fall back to the inputs
        doc_id = _extract_doc_id(parsed, record)

        # Extract the tree structure and compute metadata
        tree_meta = _extract_tree_metadata(parsed)

        record["pageindex.doc_id"] = doc_id
        record["pageindex.retrieval_method"] = "tree_search"
        record["pageindex.tree.depth"] = tree_meta.get("depth", 0)
        record["pageindex.tree.nodes_visited"] = tree_meta.get("nodes_visited", 0)
        record["pageindex.tree.path"] = tree_meta.get("path", "")
        record["pageindex.tree.backtrack_count"] = 0


# Private helpers

def _extract_output_str(record: Dict[str, Any]) -> str:
    """Extract the raw output string from a decision record."""
    outputs = record.get("outputs") or {}
    if not isinstance(outputs, dict):
        return ""
    return outputs.get("output", "") or outputs.get("content", "") or ""


def _try_parse_json(text: str) -> Optional[Any]:
    """Parse text as JSON; returns None on failure."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _extract_doc_id(
    parsed: Optional[Dict[str, Any]], record: Dict[str, Any]
) -> str:
    """Extract doc_id from the parsed output, then the inputs, else ''."""
    if isinstance(parsed, dict):
        doc_id = parsed.get("doc_id", "")
        if doc_id:
            return str(doc_id)

    inputs = record.get("inputs") or {}
    if isinstance(inputs, dict):
        input_str = inputs.get("input", "") or ""
        input_parsed = _try_parse_json(input_str)
        if isinstance(input_parsed, dict):
            doc_id = input_parsed.get("doc_id", "")
            if doc_id:
                return str(doc_id)

    return ""


def _compute_tree_depth(node: Dict[str, Any], current_depth: int = 0) -> int:
    """Recursively compute maximum depth. Children live under the 'nodes' key."""
    children = node.get("nodes", [])
    if not children:
        return current_depth
    return max(_compute_tree_depth(child, current_depth + 1) for child in children)


def _count_tree_nodes(node: Dict[str, Any]) -> int:
    """Recursively count all nodes, root inclusive."""
    children = node.get("nodes", [])
    return 1 + sum(_count_tree_nodes(child) for child in children)


def _build_tree_path(node: Dict[str, Any], max_sections: int = 3) -> str:
    """Build a readable traversal path from the root."""
    parts = []
    root_title = node.get("title") or node.get("node_id") or "root"
    parts.append(str(root_title))

    children = node.get("nodes", [])
    for child in children[:max_sections]:
        child_title = child.get("title") or child.get("node_id") or "node"
        parts.append(str(child_title))

    if len(children) > max_sections:
        parts.append(f"... ({len(children) - max_sections} more)")

    return " > ".join(parts)


def _extract_tree_metadata(parsed: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract tree depth, node count, and path from a parsed MCP response.

    Returns {} when the tree structure is absent or malformed.
    """
    if not isinstance(parsed, dict):
        return {}

    try:
        # The tree nests under a 'tree' key, or sits at the root via 'nodes'
        tree: Optional[Dict[str, Any]] = None
        if isinstance(parsed.get("tree"), dict):
            tree = parsed["tree"]
        elif "nodes" in parsed and isinstance(parsed.get("nodes"), list):
            tree = parsed  # root-level flat tree

        if tree is None:
            return {}

        return {
            "depth": _compute_tree_depth(tree),
            "nodes_visited": _count_tree_nodes(tree),
            "path": _build_tree_path(tree),
        }
    except Exception:
        return {}
