"""Tests for the briefcase MCP server (requires the 'mcp' extra)."""

import pytest

pytest.importorskip("mcp", reason="briefcase-ai[mcp] not installed")

from briefcase.mcp import build_server  # noqa: E402  (after importorskip)


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


# -- mcp 1.x / 2.x compatibility ---------------------------------------------
#
# mcp 2.0 renamed mcp.server.fastmcp.FastMCP to mcp.server.mcpserver.MCPServer.
# briefcase.mcp.server imports whichever exists. These tests run the module's
# import block against a faked package layout for each major version, so both
# branches are covered whatever mcp release is installed.


def _load_server_module(monkeypatch, layout):
    """Import a fresh copy of briefcase.mcp.server against a faked ``mcp``.

    ``layout`` maps module name -> module object, or None to make the import
    raise ImportError (how ``sys.modules`` spells "does not exist").
    """
    import importlib.util
    import sys

    import briefcase.mcp.server as real

    for name, module in layout.items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("_briefcase_mcp_server_probe", real.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_module(name, **attrs):
    import types

    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


def test_uses_fastmcp_on_mcp_1x(monkeypatch):
    class FastMCP:
        pass

    module = _load_server_module(
        monkeypatch,
        {
            "mcp.server.fastmcp": _fake_module("mcp.server.fastmcp", FastMCP=FastMCP),
            "mcp.server.mcpserver": None,
        },
    )
    assert module.FastMCP is FastMCP


def test_uses_mcpserver_on_mcp_2x(monkeypatch):
    class MCPServer:
        pass

    module = _load_server_module(
        monkeypatch,
        {
            "mcp.server.fastmcp": None,
            "mcp.server.mcpserver": _fake_module("mcp.server.mcpserver", MCPServer=MCPServer),
        },
    )
    assert module.FastMCP is MCPServer


def test_unusable_mcp_names_the_installed_version_not_a_missing_extra(monkeypatch):
    with pytest.raises(ImportError) as excinfo:
        _load_server_module(
            monkeypatch,
            {"mcp.server.fastmcp": None, "mcp.server.mcpserver": None},
        )
    message = str(excinfo.value)
    assert "neither" in message and "MCPServer" in message
    assert "requires the 'mcp' extra" not in message


def test_absent_mcp_points_at_the_extra(monkeypatch):
    with pytest.raises(ImportError, match="requires the 'mcp' extra"):
        _load_server_module(
            monkeypatch,
            {"mcp": None, "mcp.server.fastmcp": None, "mcp.server.mcpserver": None},
        )


def test_installed_mcp_major_is_supported():
    """The server must build against the installed mcp, 1.x or 2.x."""
    from importlib.metadata import version

    major = int(version("mcp").split(".")[0])
    assert major in (1, 2), f"mcp {version('mcp')} is untested with briefcase.mcp"
    assert type(build_server()).__name__ == ("FastMCP" if major == 1 else "MCPServer")
