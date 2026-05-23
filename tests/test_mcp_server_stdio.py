"""Protocol-level integration tests for mcp_server.

These complement tests/test_mcp_server.py (which calls the tool functions
as plain Python) by actually exercising the MCP wire protocol: stdio
transport + JSON-RPC framing + tool/resource discovery + tool invocation.
A regression that breaks the tool decorator surface (parameter schema,
return serialization, FastMCP version compat) will show here but not in
the plain-Python tests.

Each test spawns the server as a subprocess via the official `mcp` SDK
stdio client, so it adds ~1s per test. Marked implicitly slow — they're
still under 5s total which is fine for the regular pytest run.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

_ROOT = Path(__file__).resolve().parents[1]
_PYTHON = _ROOT / ".venv-linux" / "bin" / "python"

# Subprocess inherits no shell-activated venv, so `uv run python` would
# fail to find `mcp`. Hardcoding the venv interpreter is bulletproof.
_PARAMS = StdioServerParameters(
    command=str(_PYTHON),
    args=["-m", "mcp_server.server"],
    cwd=str(_ROOT),
)


def _run(coro):
    return asyncio.run(coro)


def _extract(result):
    """Return the tool's return value regardless of FastMCP serialization shape.

    FastMCP puts `list[dict]` into `structuredContent['result']` but plain `dict`
    only in `content[0].text` (as JSON). Try structuredContent first; fall back
    to parsing content[0].text.
    """
    import json
    if result.structuredContent is not None:
        return result.structuredContent.get("result", result.structuredContent)
    if result.content:
        text = result.content[0].text
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return None


@pytest.fixture(scope="module", autouse=True)
def _require_venv():
    if not _PYTHON.is_file():
        pytest.skip(f"venv python not at {_PYTHON} — activate or recreate .venv-linux")


def test_initialize_returns_server_info():
    async def go():
        async with stdio_client(_PARAMS) as (r, w):
            async with ClientSession(r, w) as session:
                init = await session.initialize()
                assert init.serverInfo.name == "sdlcma"
                # version is the mcp SDK's version, not ours; just non-empty.
                assert init.serverInfo.version
    _run(go())


def test_list_tools_exposes_all_six():
    async def go():
        async with stdio_client(_PARAMS) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.list_tools()
                names = {t.name for t in result.tools}
                expected = {
                    "list_fixtures",
                    "read_fixture",
                    "list_journal_entries",
                    "read_journal_entry",
                    "list_eval_runs",
                    "promote_journal_to_fixture",
                }
                assert expected.issubset(names), f"missing: {expected - names}"
    _run(go())


def test_list_resources_and_templates():
    async def go():
        async with stdio_client(_PARAMS) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                resources = await session.list_resources()
                templates = await session.list_resource_templates()
                resource_uris = {str(r.uri) for r in resources.resources}
                template_uris = {t.uriTemplate for t in templates.resourceTemplates}
                assert "sdlcma://fixtures" in resource_uris
                assert "sdlcma://runs/{run_id}/summary" in template_uris
    _run(go())


def test_call_list_fixtures_over_wire():
    async def go():
        async with stdio_client(_PARAMS) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool("list_fixtures", {})
                assert not result.isError
                fixtures = _extract(result)
                assert isinstance(fixtures, list)
                assert any(f["fixture_id"] == "F01-off-by-one" for f in fixtures)
    _run(go())


def test_call_read_fixture_over_wire():
    async def go():
        async with stdio_client(_PARAMS) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool(
                    "read_fixture", {"fixture_id": "F01-off-by-one"}
                )
                assert not result.isError
                payload = _extract(result)
                assert payload["fixture_id"] == "F01-off-by-one"
                assert "source" in payload
                assert isinstance(payload["source"], dict)
    _run(go())


def test_call_tool_with_bad_param_signals_error():
    async def go():
        async with stdio_client(_PARAMS) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool(
                    "read_fixture", {"fixture_id": "F999-nope"}
                )
                # FastMCP wraps the ValueError into an isError=True response,
                # not a transport-level exception. Either way is acceptable;
                # what matters is the client can tell the call failed.
                assert result.isError
    _run(go())


def test_read_resource_fixtures_index():
    async def go():
        async with stdio_client(_PARAMS) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.read_resource("sdlcma://fixtures")
                assert result.contents
                text = result.contents[0].text
                assert text.startswith("# SDLCMA fixtures")
                assert "F01-off-by-one" in text
    _run(go())
