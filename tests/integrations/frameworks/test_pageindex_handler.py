"""Tests for briefcase/integrations/frameworks/pageindex_handler.py.

The pageindex package is stubbed by conftest.py; tests pass mock clients
directly, so no network access ever happens.
"""

from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from briefcase.integrations.frameworks.pageindex_handler import (
    PageIndexTracer,
    _build_tree_path,
    _compute_tree_depth,
    _count_tree_nodes,
    _normalize_doc_id,
    require_pageindex,
)


# Helpers

def _make_tree_response(tree: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap a tree node into the format returned by get_tree()."""
    return {"status": "completed", "retrieval_ready": True, "tree": tree}


def _make_chat_response(content: str = "answer text") -> Dict[str, Any]:
    return {"choices": [{"message": {"content": content}, "index": 0}]}


def _make_client(tree: Dict[str, Any], chat_content: str = "answer") -> MagicMock:
    """Build a mock PageIndexClient with preset get_tree and chat_completions."""
    client = MagicMock()
    client.chat_completions.return_value = _make_chat_response(chat_content)
    client.get_tree.return_value = _make_tree_response(tree)
    return client


# A tree with depth 2:
#   root
#     Section 1
#       Section 1.1  (depth 2 from root)
#     Section 2
SAMPLE_TREE = {
    "node_id": "root",
    "title": "Document Root",
    "nodes": [
        {
            "node_id": "s1",
            "title": "Section 1",
            "nodes": [
                {"node_id": "s1_1", "title": "Section 1.1", "nodes": []},
            ],
        },
        {"node_id": "s2", "title": "Section 2", "nodes": []},
    ],
}
# depth=2, nodes_visited=4 (root + S1 + S1.1 + S2)


# Tree depth capture

class TestCapturesTreeDepth:
    def test_captures_tree_depth(self):
        """pageindex.tree.depth equals the max depth of the tree (2 for SAMPLE_TREE)."""
        client = _make_client(SAMPLE_TREE)
        tracer = PageIndexTracer(client=client, async_capture=False)
        tracer.chat_completions(
            messages=[{"role": "user", "content": "q"}],
            doc_id="doc-depth",
        )
        records = tracer.get_records()
        assert len(records) == 1
        assert records[0]["pageindex.tree.depth"] == 2

    def test_tree_depth_flat(self):
        """depth=0 for a root with no children."""
        flat_tree = {"node_id": "root", "title": "Root", "nodes": []}
        client = _make_client(flat_tree)
        tracer = PageIndexTracer(client=client, async_capture=False)
        tracer.chat_completions(messages=[], doc_id="doc-flat")
        records = tracer.get_records()
        assert records[0]["pageindex.tree.depth"] == 0

    def test_tree_depth_single_level(self):
        """depth=1 for a root with direct children only."""
        tree = {
            "node_id": "root",
            "title": "Root",
            "nodes": [
                {"node_id": "a", "title": "A", "nodes": []},
                {"node_id": "b", "title": "B", "nodes": []},
            ],
        }
        client = _make_client(tree)
        tracer = PageIndexTracer(client=client, async_capture=False)
        tracer.chat_completions(messages=[], doc_id="doc-single")
        records = tracer.get_records()
        assert records[0]["pageindex.tree.depth"] == 1


# Node count capture

class TestCapturesNodesVisited:
    def test_captures_nodes_visited(self):
        """pageindex.tree.nodes_visited equals the total node count (4 for SAMPLE_TREE)."""
        client = _make_client(SAMPLE_TREE)
        tracer = PageIndexTracer(client=client, async_capture=False)
        tracer.chat_completions(messages=[], doc_id="doc-nodes")
        records = tracer.get_records()
        assert records[0]["pageindex.tree.nodes_visited"] == 4

    def test_nodes_visited_single_root(self):
        """A single root node gives nodes_visited=1."""
        tree = {"node_id": "root", "title": "Root", "nodes": []}
        client = _make_client(tree)
        tracer = PageIndexTracer(client=client, async_capture=False)
        tracer.chat_completions(messages=[], doc_id="doc-single-root")
        records = tracer.get_records()
        assert records[0]["pageindex.tree.nodes_visited"] == 1


# Traversal path capture

class TestCapturesTraversalPath:
    def test_captures_traversal_path(self):
        """pageindex.tree.path is a non-empty string containing the root title."""
        client = _make_client(SAMPLE_TREE)
        tracer = PageIndexTracer(client=client, async_capture=False)
        tracer.chat_completions(messages=[], doc_id="doc-path")
        records = tracer.get_records()
        path = records[0]["pageindex.tree.path"]
        assert isinstance(path, str)
        assert len(path) > 0
        assert "Document Root" in path

    def test_traversal_path_has_separator(self):
        """The path uses ' > ' as the separator between levels."""
        client = _make_client(SAMPLE_TREE)
        tracer = PageIndexTracer(client=client, async_capture=False)
        tracer.chat_completions(messages=[], doc_id="doc-sep")
        records = tracer.get_records()
        assert " > " in records[0]["pageindex.tree.path"]


# Backtrack count capture

class TestCapturesBacktrackCount:
    def test_captures_backtrack_count(self):
        """backtrack_count is always 0 (not available from the PageIndex API)."""
        client = _make_client(SAMPLE_TREE)
        tracer = PageIndexTracer(client=client, async_capture=False)
        tracer.chat_completions(messages=[], doc_id="doc-bt")
        records = tracer.get_records()
        assert records[0]["pageindex.tree.backtrack_count"] == 0

    def test_backtrack_count_type_int(self):
        """backtrack_count is an integer."""
        client = _make_client(SAMPLE_TREE)
        tracer = PageIndexTracer(client=client, async_capture=False)
        tracer.chat_completions(messages=[], doc_id="doc-bt2")
        records = tracer.get_records()
        assert isinstance(records[0]["pageindex.tree.backtrack_count"], int)


# doc_id capture

class TestCapturesDocId:
    def test_captures_doc_id(self):
        """pageindex.doc_id matches the doc_id passed to chat_completions()."""
        client = _make_client(SAMPLE_TREE)
        tracer = PageIndexTracer(client=client, async_capture=False)
        tracer.chat_completions(messages=[], doc_id="pi-abc123")
        records = tracer.get_records()
        assert records[0]["pageindex.doc_id"] == "pi-abc123"

    def test_captures_doc_id_list(self):
        """When doc_id is a list, the first element is stored."""
        client = _make_client(SAMPLE_TREE)
        tracer = PageIndexTracer(client=client, async_capture=False)
        tracer.chat_completions(messages=[], doc_id=["doc-a", "doc-b"])
        records = tracer.get_records()
        assert records[0]["pageindex.doc_id"] == "doc-a"

    def test_captures_doc_id_none(self):
        """When doc_id is None, an empty string is stored."""
        client = MagicMock()
        client.chat_completions.return_value = _make_chat_response()
        # fetch_tree_metadata=False so get_tree is not called with an empty doc_id
        tracer = PageIndexTracer(client=client, async_capture=False, fetch_tree_metadata=False)
        tracer.chat_completions(messages=[], doc_id=None)
        records = tracer.get_records()
        assert records[0]["pageindex.doc_id"] == ""


# Retrieval method capture

class TestCapturesRetrievalMethod:
    def test_captures_retrieval_method(self):
        """pageindex.retrieval_method is 'tree_search'."""
        client = _make_client(SAMPLE_TREE)
        tracer = PageIndexTracer(client=client, async_capture=False)
        tracer.chat_completions(messages=[], doc_id="doc-rm")
        records = tracer.get_records()
        assert records[0]["pageindex.retrieval_method"] == "tree_search"


# Wire shape

class TestWireShape:
    def test_record_uses_wire_field_names(self):
        """Records carry the decision-record core field names."""
        client = _make_client(SAMPLE_TREE)
        tracer = PageIndexTracer(client=client, async_capture=False)
        tracer.chat_completions(messages=[], doc_id="doc-wire")
        record = tracer.get_records()[0]
        for key in ("decision_id", "decision_type", "function_name", "inputs",
                    "outputs", "started_at", "ended_at", "execution_time_ms"):
            assert key in record, key
        assert record["decision_type"] == "pageindex_retrieval"


# Graceful degradation without pageindex

class TestGracefulWithoutPageindex:
    def test_graceful_without_pageindex(self, monkeypatch):
        """Without pageindex, instantiation succeeds and wrapped calls raise ImportError."""
        import briefcase.integrations.frameworks.pageindex_handler as ph
        monkeypatch.setattr(ph, "_PAGEINDEX_AVAILABLE", False)
        monkeypatch.setattr(ph, "_PageIndexClient", None)

        # Instantiation without api_key succeeds (no client created)
        tracer = PageIndexTracer(api_key=None)
        assert tracer._client is None

        # Calling chat_completions raises ImportError
        with pytest.raises(ImportError, match="pageindex"):
            tracer.chat_completions(messages=[], doc_id="x")

    def test_graceful_with_direct_client(self, monkeypatch):
        """With a direct client, the tracer works even when pageindex is absent."""
        import briefcase.integrations.frameworks.pageindex_handler as ph
        monkeypatch.setattr(ph, "_PAGEINDEX_AVAILABLE", False)
        mock_client = _make_client(SAMPLE_TREE)
        tracer = PageIndexTracer(client=mock_client, async_capture=False)
        tracer.chat_completions(messages=[], doc_id="doc-x")
        assert len(tracer.get_records()) == 1

    def test_require_pageindex_raises(self, monkeypatch):
        """require_pageindex() raises ImportError when the package is absent."""
        import briefcase.integrations.frameworks.pageindex_handler as ph
        monkeypatch.setattr(ph, "_PAGEINDEX_AVAILABLE", False)
        with pytest.raises(ImportError, match="pageindex"):
            require_pageindex()

    def test_require_pageindex_passes(self, monkeypatch):
        """require_pageindex() does not raise when the package is present."""
        import briefcase.integrations.frameworks.pageindex_handler as ph
        monkeypatch.setattr(ph, "_PAGEINDEX_AVAILABLE", True)
        require_pageindex()  # must not raise


# Async non-blocking

class TestAsyncNoBlock:
    def test_async_no_block(self):
        """With async_capture=True, chat_completions() returns before export completes."""
        import asyncio
        import threading
        import time

        export_started = threading.Event()
        export_may_finish = threading.Event()

        async def slow_export(record):
            export_started.set()
            for _ in range(50):
                await asyncio.sleep(0.01)
                if export_may_finish.is_set():
                    break

        mock_exporter = MagicMock()
        mock_exporter.export = slow_export

        mock_client = _make_client(SAMPLE_TREE)
        tracer = PageIndexTracer(client=mock_client, async_capture=True)

        from briefcase.config import BriefcaseConfig
        BriefcaseConfig.reset()
        config = BriefcaseConfig.get()
        config.exporter = mock_exporter

        try:
            t_start = time.monotonic()
            tracer.chat_completions(messages=[], doc_id="doc-async")
            t_end = time.monotonic()

            # The call returns quickly (< 1 s), not waiting for the export
            assert (t_end - t_start) < 1.0
        finally:
            export_may_finish.set()
            BriefcaseConfig.reset()


# Export failure is silent

class TestCaptureFailureSilent:
    def test_capture_failure_silent(self):
        """Internal export errors do not propagate to the caller."""
        async def exploding_export(record):
            raise RuntimeError("Export exploded")

        mock_exporter = MagicMock()
        mock_exporter.export = exploding_export

        mock_client = _make_client(SAMPLE_TREE)
        tracer = PageIndexTracer(client=mock_client, async_capture=False)

        from briefcase.config import BriefcaseConfig
        BriefcaseConfig.reset()
        BriefcaseConfig.get().exporter = mock_exporter

        try:
            # Must not raise despite the export failure
            result = tracer.chat_completions(messages=[], doc_id="doc-err")
            assert result is not None  # original response returned
        finally:
            BriefcaseConfig.reset()

    def test_get_tree_failure_graceful(self):
        """A get_tree() failure during metadata fetch does not raise."""
        mock_client = MagicMock()
        mock_client.chat_completions.return_value = _make_chat_response()
        mock_client.get_tree.side_effect = RuntimeError("tree fetch failed")

        tracer = PageIndexTracer(client=mock_client, async_capture=False)
        result = tracer.chat_completions(messages=[], doc_id="doc-tree-err")
        assert result is not None
        records = tracer.get_records()
        # The record still exists with default tree values
        assert records[0]["pageindex.tree.depth"] == 0
        assert records[0]["pageindex.tree.nodes_visited"] == 0

    def test_export_attribute_error_silent(self):
        """An AttributeError in the exporter does not propagate."""
        mock_client = _make_client(SAMPLE_TREE)
        tracer = PageIndexTracer(client=mock_client, async_capture=False)

        from briefcase.config import BriefcaseConfig
        BriefcaseConfig.reset()
        BriefcaseConfig.get().exporter = "not-an-exporter"  # causes AttributeError

        try:
            tracer.chat_completions(messages=[], doc_id="doc-attr")
            assert len(tracer.get_records()) == 1
        finally:
            BriefcaseConfig.reset()


# context_version linkage

class TestContextVersionLinked:
    def test_context_version_linked(self):
        """context_version appears in every decision record."""
        client = _make_client(SAMPLE_TREE)
        tracer = PageIndexTracer(
            client=client, context_version="v2.1.3", async_capture=False
        )
        tracer.chat_completions(messages=[], doc_id="doc-cv")
        records = tracer.get_records()
        assert records[0].get("context_version") == "v2.1.3"

    def test_no_context_version_when_none(self):
        """When context_version is None, the key is absent from the record."""
        client = _make_client(SAMPLE_TREE)
        tracer = PageIndexTracer(client=client, context_version=None, async_capture=False)
        tracer.chat_completions(messages=[], doc_id="doc-nocv")
        records = tracer.get_records()
        assert "context_version" not in records[0]


# Exporter integration

class TestExporterCalled:
    def test_exporter_called(self):
        """The configured exporter runs once per retrieval."""
        exported_records: List[Dict] = []

        async def capturing_export(record):
            exported_records.append(record)

        mock_exporter = MagicMock()
        mock_exporter.export = capturing_export

        mock_client = _make_client(SAMPLE_TREE)
        tracer = PageIndexTracer(client=mock_client, async_capture=False)

        from briefcase.config import BriefcaseConfig
        BriefcaseConfig.reset()
        BriefcaseConfig.get().exporter = mock_exporter

        try:
            tracer.chat_completions(messages=[], doc_id="doc-exp")
            assert len(exported_records) == 1
            assert exported_records[0]["pageindex.doc_id"] == "doc-exp"
        finally:
            BriefcaseConfig.reset()

    def test_no_exporter_no_error(self):
        """With no exporter configured, no exception is raised."""
        from briefcase.config import BriefcaseConfig
        BriefcaseConfig.reset()
        BriefcaseConfig.get().exporter = None

        mock_client = _make_client(SAMPLE_TREE)
        tracer = PageIndexTracer(client=mock_client, async_capture=False)

        try:
            tracer.chat_completions(messages=[], doc_id="doc-noexp")
            assert len(tracer.get_records()) == 1
        finally:
            BriefcaseConfig.reset()


# Tree path formatting

class TestTreePathFormatting:
    def test_tree_path_formatting(self):
        """The path lists the root and child titles in order."""
        tree = {
            "node_id": "root",
            "title": "My Document",
            "nodes": [
                {"node_id": "ch1", "title": "Chapter 1", "nodes": []},
                {"node_id": "ch2", "title": "Chapter 2", "nodes": []},
            ],
        }
        path = _build_tree_path(tree)
        assert "My Document" in path
        assert "Chapter 1" in path
        assert "Chapter 2" in path
        assert path.startswith("My Document")

    def test_tree_path_overflow_suffix(self):
        """More children than max_sections adds a '... (N more)' suffix."""
        tree = {
            "node_id": "root",
            "title": "Big Doc",
            "nodes": [
                {"node_id": f"ch{i}", "title": f"Chapter {i}", "nodes": []}
                for i in range(7)
            ],
        }
        path = _build_tree_path(tree, max_sections=3)
        assert "Big Doc" in path
        assert "..." in path
        assert "more" in path

    def test_tree_path_no_title_fallback(self):
        """When the title is missing, node_id is used as a fallback."""
        tree = {"node_id": "root-id", "nodes": []}
        path = _build_tree_path(tree)
        assert "root-id" in path

    def test_build_tree_path_integration(self):
        """SAMPLE_TREE's path starts with 'Document Root'."""
        path = _build_tree_path(SAMPLE_TREE)
        assert path.startswith("Document Root")
        assert "Section 1" in path
        assert " > " in path


# Helper function unit tests

class TestHelperFunctions:
    def test_compute_tree_depth_deep(self):
        """A deeply nested tree returns the correct depth."""
        tree = {
            "node_id": "r",
            "nodes": [
                {"node_id": "a", "nodes": [
                    {"node_id": "b", "nodes": [
                        {"node_id": "c", "nodes": []}
                    ]}
                ]}
            ],
        }
        assert _compute_tree_depth(tree) == 3

    def test_count_tree_nodes_empty(self):
        """A single root with no children counts 1."""
        assert _count_tree_nodes({"node_id": "r", "nodes": []}) == 1

    def test_count_tree_nodes_sample(self):
        """SAMPLE_TREE has 4 nodes total."""
        assert _count_tree_nodes(SAMPLE_TREE) == 4

    def test_normalize_doc_id_string(self):
        """A string doc_id passes through unchanged."""
        assert _normalize_doc_id("abc") == "abc"

    def test_normalize_doc_id_list(self):
        """A list doc_id returns its first element."""
        assert _normalize_doc_id(["x", "y"]) == "x"

    def test_normalize_doc_id_empty_list(self):
        """An empty list gives an empty string."""
        assert _normalize_doc_id([]) == ""

    def test_normalize_doc_id_none(self):
        """None gives an empty string."""
        assert _normalize_doc_id(None) == ""

    def test_fetch_tree_meta_no_doc_id(self):
        """_fetch_tree_meta with an empty doc_id returns an empty dict."""
        tracer = PageIndexTracer(client=MagicMock(), async_capture=False)
        result = tracer._fetch_tree_meta("")
        assert result == {}

    def test_fetch_tree_meta_no_client(self, monkeypatch):
        """_fetch_tree_meta with no client returns an empty dict."""
        import briefcase.integrations.frameworks.pageindex_handler as ph
        monkeypatch.setattr(ph, "_PAGEINDEX_AVAILABLE", False)
        tracer = PageIndexTracer(async_capture=False)
        result = tracer._fetch_tree_meta("doc-id")
        assert result == {}

    def test_fetch_tree_meta_data_key(self):
        """A 'data' key in the tree response works as a fallback for 'tree'."""
        mock_client = MagicMock()
        mock_client.get_tree.return_value = {
            "data": {"node_id": "root", "title": "Root", "nodes": []}
        }
        tracer = PageIndexTracer(client=mock_client, async_capture=False)
        result = tracer._fetch_tree_meta("doc-x")
        assert result["pageindex.tree.depth"] == 0
        assert result["pageindex.tree.nodes_visited"] == 1

    def test_fetch_tree_meta_bad_response(self):
        """A non-dict tree value in the response gives an empty dict."""
        mock_client = MagicMock()
        mock_client.get_tree.return_value = {"tree": None}
        tracer = PageIndexTracer(client=mock_client, async_capture=False)
        result = tracer._fetch_tree_meta("doc-bad")
        assert result == {}

    def test_get_records_empty_initially(self):
        """No records exist before any calls."""
        tracer = PageIndexTracer(client=MagicMock(), async_capture=False)
        assert tracer.get_records() == []

    def test_clear_resets_records(self):
        """clear() empties the record list."""
        client = _make_client(SAMPLE_TREE)
        tracer = PageIndexTracer(client=client, async_capture=False)
        tracer.chat_completions(messages=[], doc_id="doc-clear")
        assert len(tracer.get_records()) == 1
        tracer.clear()
        assert tracer.get_records() == []

    def test_get_tree_raises_without_client(self, monkeypatch):
        """get_tree() raises ImportError when no client is set."""
        import briefcase.integrations.frameworks.pageindex_handler as ph
        monkeypatch.setattr(ph, "_PAGEINDEX_AVAILABLE", False)
        tracer = PageIndexTracer(async_capture=False)
        with pytest.raises(ImportError, match="pageindex"):
            tracer.get_tree("doc-id")

    def test_chat_completions_raises_without_client(self, monkeypatch):
        """chat_completions() raises ImportError when no client is set."""
        import briefcase.integrations.frameworks.pageindex_handler as ph
        monkeypatch.setattr(ph, "_PAGEINDEX_AVAILABLE", False)
        tracer = PageIndexTracer(async_capture=False)
        with pytest.raises(ImportError, match="pageindex"):
            tracer.chat_completions(messages=[], doc_id="x")

    def test_get_tree_delegates_to_client(self):
        """get_tree() passes through to the underlying client."""
        mock_client = MagicMock()
        mock_client.get_tree.return_value = {"tree": {}}
        tracer = PageIndexTracer(client=mock_client, async_capture=False)
        tracer.get_tree("doc-delegate", node_summary=True)
        mock_client.get_tree.assert_called_once_with("doc-delegate", node_summary=True)


# Error propagation

class TestErrorPropagation:
    def test_api_error_propagates(self):
        """When chat_completions() raises, the exception re-raises after recording."""
        mock_client = MagicMock()
        mock_client.chat_completions.side_effect = ValueError("API error")

        tracer = PageIndexTracer(client=mock_client, async_capture=False, fetch_tree_metadata=False)
        with pytest.raises(ValueError, match="API error"):
            tracer.chat_completions(messages=[], doc_id="doc-apierr")

        # The error decision is still recorded
        records = tracer.get_records()
        assert len(records) == 1
        assert "API error" in records[0]["error"]

    def test_fetch_tree_disabled(self):
        """With fetch_tree_metadata=False, get_tree() is never called."""
        mock_client = MagicMock()
        mock_client.chat_completions.return_value = _make_chat_response()
        tracer = PageIndexTracer(
            client=mock_client, async_capture=False, fetch_tree_metadata=False
        )
        tracer.chat_completions(messages=[], doc_id="doc-notree")
        mock_client.get_tree.assert_not_called()
        records = tracer.get_records()
        # Defaults still present
        assert records[0]["pageindex.tree.depth"] == 0
