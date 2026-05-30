"""Tests for the briefcase MCP server (requires the 'mcp' extra)."""

import pytest

pytest.importorskip("mcp", reason="briefcase-ai[mcp] not installed")

from briefcase.mcp import build_server


def test_server_builds():
    assert build_server() is not None


@pytest.mark.asyncio
async def test_tools_and_resources_registered():
    server = build_server()
    tool_names = {t.name for t in await server.list_tools()}
    assert {"sanitize_text", "estimate_cost", "analyze_drift", "how_to"} <= tool_names
    resource_uris = {str(r.uri) for r in await server.list_resources()}
    assert any("llms-full" in uri for uri in resource_uris)


@pytest.mark.asyncio
async def test_sanitize_tool_is_callable():
    # Under the test suite briefcase._native is mocked (see tests/mock_core.py),
    # so we assert the tool is wired (returns the sanitized/redactions shape)
    # rather than the native redaction itself (covered by bindings/python/tests).
    server = build_server()
    result = await server.call_tool("sanitize_text", {"text": "reach me at a@b.com"})
    assert "sanitized" in str(result) and "redactions" in str(result)


@pytest.mark.asyncio
async def test_how_to_tool_returns_guidance():
    server = build_server()
    result = await server.call_tool("how_to", {"topic": "sanitize"})
    assert "Sanitizer" in str(result) or "sanitize" in str(result).lower()
