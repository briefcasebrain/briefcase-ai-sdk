"""Tests for briefcase/integrations/frameworks/pageindex_mcp.py.

The observer parses JSON only, so no framework stubs are exercised here.
"""

import json
from typing import Any, Dict

from briefcase.integrations.frameworks.pageindex_mcp import (
    PageIndexMCPObserver,
    _build_tree_path,
    _compute_tree_depth,
    _count_tree_nodes,
    _extract_doc_id,
    _extract_output_str,
    _extract_tree_metadata,
    _try_parse_json,
)


# Fixture helpers

def _tool_record(
    name: str = "pageindex_query",
    output: str = "",
    inputs: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """Build a minimal tool decision record."""
    return {
        "decision_type": "tool",
        "function_name": name,
        "inputs": inputs or {"input": ""},
        "outputs": {"output": output},
    }


def _pi_response(doc_id: str = "doc-123", content: str = "result text") -> str:
    """Build a JSON string resembling a PageIndex MCP response."""
    return json.dumps({"doc_id": doc_id, "content": content})


SAMPLE_TREE = {
    "node_id": "root",
    "title": "My Document",
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
# depth=2, nodes=4


def _pi_tree_response(doc_id: str = "doc-tree") -> str:
    """An MCP response that includes a tree structure."""
    return json.dumps({"doc_id": doc_id, "tree": SAMPLE_TREE})


# Detection

class TestDetectsPageindexMcpResponse:
    def test_detects_pageindex_mcp_response(self):
        """A name containing 'pageindex' is detected."""
        observer = PageIndexMCPObserver()
        record = _tool_record(name="pageindex_search", output=_pi_response())
        assert observer.is_pageindex_mcp_response(record) is True

    def test_detects_by_pi_search_keyword(self):
        """A tool named 'pi_search' is detected."""
        observer = PageIndexMCPObserver()
        record = _tool_record(name="pi_search", output=_pi_response())
        assert observer.is_pageindex_mcp_response(record) is True

    def test_detects_by_pi_chat_keyword(self):
        """A tool named 'pi_chat' is detected."""
        observer = PageIndexMCPObserver()
        record = _tool_record(name="pi_chat", output=_pi_response())
        assert observer.is_pageindex_mcp_response(record) is True

    def test_detects_by_pi_retrieve_keyword(self):
        """A tool named 'pi_retrieve' is detected."""
        observer = PageIndexMCPObserver()
        record = _tool_record(name="pi_retrieve", output=_pi_response())
        assert observer.is_pageindex_mcp_response(record) is True

    def test_detects_by_page_index_keyword(self):
        """A tool named 'page_index_query' is detected."""
        observer = PageIndexMCPObserver()
        record = _tool_record(name="page_index_query", output=_pi_response())
        assert observer.is_pageindex_mcp_response(record) is True

    def test_detects_by_doc_id_in_output(self):
        """JSON containing 'doc_id' is detected even with a generic tool name."""
        observer = PageIndexMCPObserver()
        record = _tool_record(name="search_docs", output=_pi_response())
        assert observer.is_pageindex_mcp_response(record) is True

    def test_detects_by_retrieval_id_in_output(self):
        """JSON containing 'retrieval_id' is detected."""
        observer = PageIndexMCPObserver()
        output = json.dumps({"retrieval_id": "ret-abc"})
        record = _tool_record(name="generic_tool", output=output)
        assert observer.is_pageindex_mcp_response(record) is True

    def test_detects_by_nodes_in_output(self):
        """JSON containing a root-level 'nodes' list is detected."""
        observer = PageIndexMCPObserver()
        output = json.dumps({"nodes": [{"node_id": "n1"}]})
        record = _tool_record(name="generic_tool", output=output)
        assert observer.is_pageindex_mcp_response(record) is True

    def test_detects_by_nested_tree_in_output(self):
        """JSON containing a 'tree' dict is detected."""
        observer = PageIndexMCPObserver()
        output = json.dumps({"tree": {"node_id": "root", "nodes": []}})
        record = _tool_record(name="generic_tool", output=output)
        assert observer.is_pageindex_mcp_response(record) is True


# Tree metadata extraction

class TestExtractsTreeMetadataFromMcp:
    def test_extracts_tree_metadata_from_mcp(self):
        """observe() populates all pageindex.tree.* attributes from the response tree."""
        observer = PageIndexMCPObserver()
        record = _tool_record(name="pageindex_query", output=_pi_tree_response("doc-tm"))
        observer.observe(record)

        assert "pageindex.tree.depth" in record
        assert "pageindex.tree.nodes_visited" in record
        assert "pageindex.tree.path" in record
        assert "pageindex.tree.backtrack_count" in record
        # Specific values for SAMPLE_TREE
        assert record["pageindex.tree.depth"] == 2
        assert record["pageindex.tree.nodes_visited"] == 4
        assert isinstance(record["pageindex.tree.path"], str)
        assert len(record["pageindex.tree.path"]) > 0
        assert record["pageindex.tree.backtrack_count"] == 0

    def test_tree_depth_correct(self):
        """depth=2 for SAMPLE_TREE."""
        observer = PageIndexMCPObserver()
        record = _tool_record(name="pageindex_q", output=_pi_tree_response())
        observer.observe(record)
        assert record["pageindex.tree.depth"] == 2

    def test_nodes_visited_correct(self):
        """nodes_visited=4 for SAMPLE_TREE."""
        observer = PageIndexMCPObserver()
        record = _tool_record(name="pageindex_q", output=_pi_tree_response())
        observer.observe(record)
        assert record["pageindex.tree.nodes_visited"] == 4

    def test_path_contains_root_title(self):
        """The path includes the root title."""
        observer = PageIndexMCPObserver()
        record = _tool_record(name="pageindex_q", output=_pi_tree_response())
        observer.observe(record)
        assert "My Document" in record["pageindex.tree.path"]

    def test_tree_without_nested_structure_defaults(self):
        """A response with doc_id but no tree defaults tree attributes to zero/empty."""
        observer = PageIndexMCPObserver()
        record = _tool_record(name="pageindex_q", output=_pi_response("doc-notree"))
        observer.observe(record)
        assert record["pageindex.tree.depth"] == 0
        assert record["pageindex.tree.nodes_visited"] == 0
        assert record["pageindex.tree.path"] == ""
        assert record["pageindex.tree.backtrack_count"] == 0

    def test_flat_tree_at_root_detected_and_enriched(self):
        """A response with 'nodes' at the root (flat tree) computes metadata."""
        observer = PageIndexMCPObserver()
        flat_tree_response = json.dumps({
            "doc_id": "doc-flat",
            "nodes": [
                {"node_id": "a", "title": "A", "nodes": []},
                {"node_id": "b", "title": "B", "nodes": []},
            ],
        })
        record = _tool_record(name="generic_tool", output=flat_tree_response)
        observer.observe(record)
        # Flat tree at the root: depth=1 (root children have no children)
        assert record["pageindex.tree.depth"] == 1
        assert record["pageindex.tree.nodes_visited"] == 3  # root + A + B


# Non-matching records

class TestIgnoresNonPageindexMcp:
    def test_ignores_non_pageindex_mcp(self):
        """Records with generic tool names and no PageIndex keys are not enriched."""
        observer = PageIndexMCPObserver()
        record = _tool_record(name="weather_tool", output=json.dumps({"temp": 72}))
        result = observer.observe(record)
        assert result is False
        assert "pageindex.doc_id" not in record

    def test_ignores_plain_text_output(self):
        """Plain text output (not JSON) does not trigger enrichment."""
        observer = PageIndexMCPObserver()
        record = _tool_record(name="calculator", output="The answer is 42")
        result = observer.observe(record)
        assert result is False

    def test_ignores_llm_decision_type(self):
        """LLM records with no PageIndex markers are ignored."""
        observer = PageIndexMCPObserver()
        record = {
            "decision_type": "llm",
            "function_name": "gpt-4",
            "outputs": {"text": "Hello"},
        }
        result = observer.observe(record)
        assert result is False
        assert "pageindex.doc_id" not in record

    def test_ignores_empty_output(self):
        """Records with empty output are not enriched."""
        observer = PageIndexMCPObserver()
        record = _tool_record(name="other_tool", output="")
        result = observer.observe(record)
        assert result is False

    def test_ignores_json_without_pi_keys(self):
        """JSON without doc_id/retrieval_id/nodes/tree is ignored."""
        observer = PageIndexMCPObserver()
        record = _tool_record(
            name="search",
            output=json.dumps({"result": "found", "score": 0.9}),
        )
        result = observer.observe(record)
        assert result is False


# In-place enrichment

class TestEnrichesExistingDecisionRecord:
    def test_enriches_existing_decision_record(self):
        """observe() adds pageindex.* keys to an existing record dict."""
        observer = PageIndexMCPObserver()
        record = _tool_record(name="pageindex_chat", output=_pi_response("doc-enrich"))
        record["existing_key"] = "should stay"

        result = observer.observe(record)
        assert result is True
        # Existing keys untouched
        assert record["existing_key"] == "should stay"
        assert record["function_name"] == "pageindex_chat"
        # New keys added
        assert record["pageindex.doc_id"] == "doc-enrich"
        assert record["pageindex.retrieval_method"] == "tree_search"
        assert "pageindex.tree.depth" in record

    def test_doc_id_extracted_from_output(self):
        """doc_id is taken from the JSON output."""
        observer = PageIndexMCPObserver()
        record = _tool_record(name="pageindex_q", output=_pi_response("doc-abc"))
        observer.observe(record)
        assert record["pageindex.doc_id"] == "doc-abc"

    def test_doc_id_fallback_from_inputs(self):
        """When the output has no doc_id, the inputs are checked."""
        observer = PageIndexMCPObserver()
        input_payload = json.dumps({"doc_id": "input-doc-id"})
        record = {
            "function_name": "pageindex_q",
            "inputs": {"input": input_payload},
            "outputs": {"output": json.dumps({"retrieval_id": "ret-xyz"})},
        }
        observer.observe(record)
        # The output has retrieval_id (no doc_id), so it falls back to the input
        assert record["pageindex.doc_id"] == "input-doc-id"

    def test_observe_returns_true_on_enrichment(self):
        """observe() returns True when the record was enriched."""
        observer = PageIndexMCPObserver()
        record = _tool_record(name="pageindex_q", output=_pi_response())
        result = observer.observe(record)
        assert result is True

    def test_observed_count_increments(self):
        """observed_count increments with each call to observe()."""
        observer = PageIndexMCPObserver()
        record1 = _tool_record(name="pageindex_q", output=_pi_response())
        record2 = _tool_record(name="weather", output="sunny")
        observer.observe(record1)
        observer.observe(record2)
        assert observer.observed_count == 2

    def test_enriched_count_increments_only_for_pi(self):
        """enriched_count increments only for matched records."""
        observer = PageIndexMCPObserver()
        observer.observe(_tool_record(name="pageindex_q", output=_pi_response()))
        observer.observe(_tool_record(name="weather", output="sunny"))
        observer.observe(_tool_record(name="pi_search", output=_pi_response()))
        assert observer.enriched_count == 2

    def test_tool_name_in_span_data(self):
        """A tool_name key (OpenAI Agents span shape) is also detected."""
        observer = PageIndexMCPObserver()
        record = {
            "tool_name": "pageindex_query",
            "inputs": {"input": ""},
            "outputs": {"output": _pi_response()},
        }
        assert observer.is_pageindex_mcp_response(record) is True


# No pageindex install required

class TestWorksWithoutPageindexInstalled:
    def test_works_without_pageindex_installed(self):
        """The observer works with no pageindex package present; it only parses JSON."""
        import importlib
        import sys

        pageindex_module = sys.modules.pop("pageindex", None)
        try:
            import briefcase.integrations.frameworks.pageindex_mcp as mcp_mod
            importlib.reload(mcp_mod)
            observer = mcp_mod.PageIndexMCPObserver()
            record = _tool_record(name="pageindex_chat", output=_pi_response())
            result = observer.observe(record)
            assert result is True
            assert record["pageindex.doc_id"] == "doc-123"
        finally:
            if pageindex_module is not None:
                sys.modules["pageindex"] = pageindex_module


# Malformed responses

class TestHandlesMalformedMcpResponse:
    def test_handles_malformed_mcp_response(self):
        """Malformed JSON output does not raise; enrichment uses defaults."""
        observer = PageIndexMCPObserver()
        record = _tool_record(name="pageindex_q", output="{invalid-json")
        result = observer.observe(record)
        assert result is True  # detected by name
        assert record["pageindex.doc_id"] == ""
        assert record["pageindex.tree.depth"] == 0

    def test_handles_non_dict_json_output(self):
        """Output that is valid JSON but not a dict is handled."""
        observer = PageIndexMCPObserver()
        # A JSON array: not a PageIndex response by content, but the name matches
        record = _tool_record(name="pageindex_q", output=json.dumps([1, 2, 3]))
        result = observer.observe(record)
        assert result is True

    def test_handles_null_output(self):
        """JSON null output does not raise."""
        observer = PageIndexMCPObserver()
        record = _tool_record(name="pageindex_q", output="null")
        result = observer.observe(record)
        assert result is True  # detected by name
        assert record["pageindex.tree.depth"] == 0

    def test_handles_missing_outputs_key(self):
        """A record without an 'outputs' key does not raise."""
        observer = PageIndexMCPObserver()
        record = {"function_name": "pageindex_chat"}
        result = observer.observe(record)
        assert result is True
        assert record["pageindex.doc_id"] == ""

    def test_handles_none_outputs(self):
        """A record with outputs=None does not raise."""
        observer = PageIndexMCPObserver()
        record = {"function_name": "pageindex_chat", "outputs": None}
        result = observer.observe(record)
        assert result is True

    def test_handles_completely_empty_record(self):
        """An empty record is handled without raising."""
        observer = PageIndexMCPObserver()
        result = observer.observe({})
        assert result is False  # no name, no content markers

    def test_malformed_inputs_does_not_raise(self):
        """Malformed JSON in the inputs does not raise."""
        observer = PageIndexMCPObserver()
        record = {
            "function_name": "pageindex_q",
            "inputs": {"input": "{bad-json"},
            "outputs": {"output": json.dumps({"retrieval_id": "ret-1"})},
        }
        result = observer.observe(record)
        assert result is True  # detected by retrieval_id
        assert record["pageindex.doc_id"] == ""  # input parse failed, no doc_id in output


# Synchronous speed sanity check

class TestObserveIsFast:
    def test_observe_is_fast(self):
        """observe() is synchronous and returns quickly."""
        import time
        observer = PageIndexMCPObserver()
        record = _tool_record(name="pageindex_q", output=_pi_tree_response())

        t_start = time.monotonic()
        for _ in range(100):
            observer.observe(record.copy())
        t_end = time.monotonic()

        # 100 observations complete in under 1 second
        assert (t_end - t_start) < 1.0


# Failure silence

class TestCaptureFailureSilent:
    def test_capture_failure_silent(self):
        """When internal processing raises, observe() returns False without propagating."""
        observer = PageIndexMCPObserver()

        def exploding_enrich(record):
            raise RuntimeError("Boom")

        observer._enrich_record = exploding_enrich

        record = _tool_record(name="pageindex_q", output=_pi_response())
        result = observer.observe(record)
        # Must not raise; returns False because enrichment failed
        assert result is False
        # The record was not mutated
        assert "pageindex.doc_id" not in record

    def test_is_pageindex_mcp_response_silent(self):
        """is_pageindex_mcp_response() never raises even on bad input."""
        observer = PageIndexMCPObserver()
        result = observer.is_pageindex_mcp_response(None)  # type: ignore
        assert result is False


# Helper function unit tests

class TestHelperFunctions:
    def test_try_parse_json_valid(self):
        """Valid JSON parses correctly."""
        result = _try_parse_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_try_parse_json_invalid(self):
        """Invalid JSON returns None."""
        assert _try_parse_json("{bad}") is None

    def test_try_parse_json_empty(self):
        """An empty string returns None."""
        assert _try_parse_json("") is None

    def test_try_parse_json_none(self):
        """None returns None without raising."""
        assert _try_parse_json(None) is None  # type: ignore

    def test_extract_output_str_output_key(self):
        """Extracts the 'output' key from the outputs dict."""
        record = {"outputs": {"output": "hello"}}
        assert _extract_output_str(record) == "hello"

    def test_extract_output_str_content_key(self):
        """Falls back to the 'content' key when 'output' is absent."""
        record = {"outputs": {"content": "world"}}
        assert _extract_output_str(record) == "world"

    def test_extract_output_str_missing(self):
        """Returns an empty string when outputs is absent."""
        assert _extract_output_str({}) == ""

    def test_extract_output_str_none_outputs(self):
        """Returns an empty string when outputs is None."""
        record = {"outputs": None}
        assert _extract_output_str(record) == ""

    def test_extract_doc_id_from_parsed(self):
        """doc_id is taken from the parsed dict."""
        assert _extract_doc_id({"doc_id": "abc"}, {}) == "abc"

    def test_extract_doc_id_fallback_inputs(self):
        """Falls back to the inputs when the parsed output has no doc_id."""
        record = {"inputs": {"input": json.dumps({"doc_id": "from-input"})}}
        assert _extract_doc_id(None, record) == "from-input"

    def test_extract_doc_id_empty(self):
        """Returns an empty string when neither source has a doc_id."""
        assert _extract_doc_id(None, {}) == ""

    def test_compute_tree_depth_leaf(self):
        """A leaf node has depth 0."""
        node = {"node_id": "leaf", "nodes": []}
        assert _compute_tree_depth(node) == 0

    def test_compute_tree_depth_sample(self):
        """SAMPLE_TREE has depth 2."""
        assert _compute_tree_depth(SAMPLE_TREE) == 2

    def test_count_tree_nodes_leaf(self):
        """A single node counts 1."""
        assert _count_tree_nodes({"node_id": "x", "nodes": []}) == 1

    def test_count_tree_nodes_sample(self):
        """SAMPLE_TREE has 4 nodes."""
        assert _count_tree_nodes(SAMPLE_TREE) == 4

    def test_build_tree_path_basic(self):
        """The path includes the root title."""
        path = _build_tree_path({"title": "Root", "nodes": []})
        assert path == "Root"

    def test_build_tree_path_with_children(self):
        """The path includes child titles."""
        node = {
            "title": "Doc",
            "nodes": [
                {"title": "Ch1", "nodes": []},
                {"title": "Ch2", "nodes": []},
            ],
        }
        path = _build_tree_path(node)
        assert "Doc" in path
        assert "Ch1" in path

    def test_build_tree_path_overflow(self):
        """More than max_sections children shows '... (N more)'."""
        node = {
            "title": "R",
            "nodes": [{"title": f"C{i}", "nodes": []} for i in range(6)],
        }
        path = _build_tree_path(node, max_sections=3)
        assert "..." in path
        assert "more" in path

    def test_extract_tree_metadata_none(self):
        """None input gives an empty dict."""
        assert _extract_tree_metadata(None) == {}

    def test_extract_tree_metadata_no_tree_key(self):
        """A dict with no tree/nodes gives an empty dict."""
        assert _extract_tree_metadata({"content": "text"}) == {}

    def test_extract_tree_metadata_nested_tree(self):
        """A nested 'tree' key triggers metadata extraction."""
        parsed = {"tree": {"node_id": "root", "title": "R", "nodes": []}}
        meta = _extract_tree_metadata(parsed)
        assert meta["depth"] == 0
        assert meta["nodes_visited"] == 1

    def test_extract_tree_metadata_flat_nodes(self):
        """A root-level 'nodes' list triggers metadata extraction."""
        parsed = {
            "title": "R",
            "nodes": [{"node_id": "c", "title": "C", "nodes": []}],
        }
        meta = _extract_tree_metadata(parsed)
        assert meta["depth"] == 1
        assert meta["nodes_visited"] == 2
