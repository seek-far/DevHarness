"""Connect to mcp_server.server over stdio and list tools/resources.

The fastest way to verify the MCP server is wire-correct without standing
up Claude Desktop or MCP Inspector. Uses the official `mcp` SDK's stdio
client, so it exercises the same JSON-RPC framing any real client would.

Run:
    .venv-linux/bin/python -m mcp_server.probe_stdio

Or via uv after activating the venv:
    source .venv-linux/bin/activate
    uv run python -m mcp_server.probe_stdio
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

_ROOT = Path(__file__).resolve().parent.parent
# Use the venv python directly: a subprocess spawned by the MCP client
# inherits no shell-activated venv, so `uv run python` fails to find the
# `mcp` import. Pointing at the venv interpreter is the bulletproof recipe.
_PYTHON = _ROOT / ".venv-linux" / "bin" / "python"


async def probe() -> int:
    params = StdioServerParameters(
        command=str(_PYTHON),
        args=["-m", "mcp_server.server"],
        cwd=str(_ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"=== server: {init.serverInfo.name} v{init.serverInfo.version} ===\n")

            tools = await session.list_tools()
            print(f"TOOLS ({len(tools.tools)}):")
            for t in tools.tools:
                params_list = list(t.inputSchema.get("properties", {}).keys())
                desc = (t.description or "").splitlines()[0]
                print(f"  {t.name}({', '.join(params_list)}) — {desc}")

            templates = await session.list_resource_templates()
            print(f"\nRESOURCE TEMPLATES ({len(templates.resourceTemplates)}):")
            for r in templates.resourceTemplates:
                print(f"  {r.uriTemplate} — {r.name}")

            resources = await session.list_resources()
            print(f"\nRESOURCES ({len(resources.resources)}):")
            for r in resources.resources:
                print(f"  {r.uri} — {r.name}")

            # One real call to prove tool execution works over the wire.
            print("\nlist_fixtures() call:")
            result = await session.call_tool("list_fixtures", {})
            if result.isError:
                print(f"  ERROR: {result.content}")
                return 1
            # FastMCP puts list-returning tools in structuredContent['result'].
            fixtures = result.structuredContent["result"]  # type: ignore[index]
            print(f"  → {len(fixtures)} fixtures returned")
            for f in fixtures[:3]:
                print(f"    {f['fixture_id']} ({f['category']}, {f['difficulty']})")
            if len(fixtures) > 3:
                print(f"    … and {len(fixtures) - 3} more")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(probe()))
