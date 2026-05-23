# SDLCMA MCP server

A [Model Context Protocol](https://modelcontextprotocol.io) server that
exposes SDLCMA's evaluation state — curated fixtures, the running-mode
journal, and evaluation sweep runs — as MCP tools and resources, plus one
mutating tool to promote a journal entry into a fixture.

Pairs with the `evaluation/` subsystem. An MCP-aware client (Claude Desktop,
mcp-cli, an MCP-aware agent) can:

- inspect what fixtures and journal entries exist,
- read the source / trace / RunRecord of a specific entry,
- compare sweep runs (fix_rate, agents in play, record counts),
- act: promote a flagged journal entry into a fixture.

## Tools

| Tool | Side effect | Purpose |
|---|---|---|
| `list_fixtures()` | none | All curated fixtures with category / difficulty / notes |
| `read_fixture(fixture_id)` | none | meta + source-file map (capped at 50 KB/file) + trace |
| `list_journal_entries(flagged_only=False, limit=20)` | none | Recent running-mode entries, newest first |
| `read_journal_entry(entry_id)` | none | Full RunRecord + (truncated) trace + test output |
| `list_eval_runs(limit=10)` | none | Recent evaluation sweep runs, with agents / fix_count |
| `promote_journal_to_fixture(entry_id, fixture_id, category, difficulty, source_repo?)` | **writes** `evaluation/fixtures/<id>/` | Delegates to `bench promote` (the existing CLI flow) |

## Resources

| URI | Purpose |
|---|---|
| `sdlcma://fixtures` | Markdown index of all fixtures |
| `sdlcma://runs/{run_id}/summary` | Raw `summary.json` for one sweep |

## Verify it works (no external client needed)

```bash
source .venv-linux/bin/activate
uv run python -m mcp_server.probe_stdio
```

This launches the server over stdio via the official `mcp` SDK client,
lists tools + resources + templates, and actually calls `list_fixtures`
to prove tool invocation works. Equivalent automated coverage lives in
`tests/test_mcp_server_stdio.py`.

## Run as a server (raw stdio, no client wired)

```bash
source .venv-linux/bin/activate
uv run python -m mcp_server.server
# (blocks waiting for JSON-RPC frames on stdin; Ctrl-C to exit)
```

This is what an MCP client will spawn internally — most of the time you
don't run it directly.

## Interactive testing options

| Tool | Setup | What you get |
|---|---|---|
| `probe_stdio.py` (in-repo) | nothing | One-shot listing + one tool call |
| `mcp dev mcp_server/server.py` | `pip install "mcp[cli]"` | Launches MCP Inspector wired to your server |
| **MCP Inspector** via npx | `npx @modelcontextprotocol/inspector …` | Browser UI: click tools, see schemas, send calls |
| **Claude Desktop** | edit `claude_desktop_config.json` | Natural-language LLM ↔ MCP demo |

### MCP Inspector via npx

```bash
npx @modelcontextprotocol/inspector \
  /abs/path/to/sdlcma/v08/.venv-linux/bin/python \
  -m mcp_server.server
```

(Why venv-python rather than `uv run`: a subprocess spawned by the
Inspector inherits no shell-activated venv, so `uv run` cannot find
the `mcp` import. The venv interpreter is the bulletproof recipe; same
constraint applies to Claude Desktop config below.)

Opens a browser UI. Click "Tools" → pick `list_fixtures` → "Run". Great
for showing the server in a demo / interview.

### Claude Desktop

Add to `claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`,
Windows: `%APPDATA%/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "sdlcma": {
      "command": "/abs/path/to/sdlcma/v08/.venv-linux/bin/python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/abs/path/to/sdlcma/v08"
    }
  }
}
```

Restart Claude Desktop. The 🔨 icon should now list `sdlcma`'s tools
and you can ask "list all fixtures grouped by category" or "which
journal entries are flagged and why?" in natural language.

### NOT recommended

`uvx mcp-cli --server "uv run python -m mcp_server.server"` looks
plausible but doesn't work — that `mcp-cli` is the third-party
`chuk-mcp` package, where `--server` expects a server **name** from a
config file, not a stdio command string. It silently falls back to
HTTP transport and errors out with 401 / DNS failures. Use the
official `mcp` SDK paths above (probe / `mcp dev` / Inspector) instead.

## Design notes

- **Decorator-preserved functions.** `@mcp.tool()` and `@mcp.resource()`
  leave the wrapped function callable directly. Unit tests (`tests/test_mcp_server.py`)
  exercise the functions without standing up a server.
- **Read-only first.** Only `promote_journal_to_fixture` mutates, and it
  delegates to the existing `bench promote` flow — no duplicated logic.
- **Truncation caps.** Source files are capped at 50 KB and traces / test
  output at 20 KB so a pathological entry doesn't blow the JSON-RPC
  response or a client UI.
- **Transport.** Stdio only for now (works with Claude Desktop + mcp-cli).
  HTTP/SSE is a small follow-up if a remote client ever needs it.
