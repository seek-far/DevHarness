"""MCP server exposing SDLCMA's evaluation state as tools + resources.

Pairs with the `evaluation/` subsystem: lets an MCP-aware client (Claude
Desktop, mcp-cli, an MCP-aware agent) introspect fixtures, journal entries,
and sweep runs, and act on them (promote a journal entry into a fixture).

Run via stdio (default for Claude Desktop / mcp-cli):

    uv run python -m mcp_server.server

Or invoke FastMCP's helper:

    uv run python -m mcp_server.server  # __main__ calls mcp.run()

Read-only tools first; the only mutating tool is `promote_journal_to_fixture`,
which delegates to the existing `bench promote` CLI flow.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_FIXTURES = _ROOT / "evaluation" / "fixtures"
_JOURNAL = _ROOT / "evaluation" / "journal"
_RUNS = _ROOT / "evaluation" / "runs"

# Max bytes returned per file in read_fixture / read_journal_entry. MCP tool
# results travel through JSON-RPC; very large blobs blow client UIs. Trace +
# test output are usually < 20 KB anyway.
_MAX_FILE_BYTES = 50_000
_MAX_TRACE_BYTES = 20_000

mcp = FastMCP("sdlcma")


# ── Tools ────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_fixtures() -> list[dict]:
    """List all bug-fix fixtures with their metadata.

    Returns one entry per directory under evaluation/fixtures/, each with
    fixture_id, category (e.g. off-by-one, type-coercion), difficulty,
    expected_outcome, whether trace.txt / expected.patch exist, and notes.
    """
    # Local import: pulls in evaluation, which is fine for this code path.
    import sys
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    from evaluation.fixture import discover

    return [
        {
            "fixture_id": f.fixture_id,
            "category": f.category,
            "difficulty": f.difficulty,
            "expected_outcome": f.expected_outcome,
            "has_trace": f.trace_file is not None,
            "has_expected_patch": f.expected_patch is not None,
            "notes": f.notes,
        }
        for f in discover()
    ]


@mcp.tool()
def read_fixture(fixture_id: str) -> dict:
    """Return one fixture's full content: meta.json + source files + trace.

    Source files are returned as a {relative_path: content} map, each capped
    at 50 KB so a pathological fixture doesn't blow the JSON-RPC response.
    Binary files are tagged as "<binary>".
    """
    fixture_dir = _FIXTURES / fixture_id
    if not fixture_dir.is_dir():
        raise ValueError(f"no such fixture: {fixture_id}")

    meta_path = fixture_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}

    source_files: dict[str, str] = {}
    source_dir = fixture_dir / "source"
    if source_dir.is_dir():
        for f in sorted(source_dir.rglob("*")):
            if not f.is_file():
                continue
            rel = str(f.relative_to(source_dir))
            try:
                source_files[rel] = f.read_text(encoding="utf-8")[:_MAX_FILE_BYTES]
            except (OSError, UnicodeDecodeError):
                source_files[rel] = "<binary>"

    trace = None
    trace_file = fixture_dir / "trace.txt"
    if trace_file.exists():
        trace = trace_file.read_text(encoding="utf-8", errors="replace")[:_MAX_TRACE_BYTES]

    return {
        "fixture_id": fixture_id,
        "meta": meta,
        "source": source_files,
        "trace": trace,
    }


@mcp.tool()
def list_journal_entries(flagged_only: bool = False, limit: int = 20) -> list[dict]:
    """List recent journal entries (one per bug-fix run, newest first).

    Each running-mode invocation writes a directory under evaluation/journal/.
    flagged_only=True filters to entries with the FLAGGED marker (failures /
    no_fix / >=2 iterations) — candidates worth promoting into the benchmark.
    """
    if not _JOURNAL.is_dir():
        return []

    out: list[dict] = []
    for entry in sorted(_JOURNAL.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        flagged = (entry / "FLAGGED").exists()
        if flagged_only and not flagged:
            continue
        record_path = entry / "record.json"
        if not record_path.exists():
            continue
        try:
            rec = json.loads(record_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        out.append({
            "entry_id": entry.name,
            "flagged": flagged,
            "agent": rec.get("agent"),
            "outcome": rec.get("outcome"),
            "iterations": rec.get("iterations"),
            "timestamp": rec.get("timestamp"),
            "bug_id": rec.get("bug_id"),
        })
        if len(out) >= limit:
            break
    return out


@mcp.tool()
def read_journal_entry(entry_id: str) -> dict:
    """Return a single journal entry's full record + (truncated) trace + test output."""
    entry_dir = _JOURNAL / entry_id
    if not entry_dir.is_dir():
        raise ValueError(f"no such journal entry: {entry_id}")

    record = json.loads((entry_dir / "record.json").read_text(encoding="utf-8"))

    def _read_truncated(p: Path, cap: int) -> str | None:
        if not p.exists():
            return None
        return p.read_text(encoding="utf-8", errors="replace")[:cap]

    return {
        "entry_id": entry_id,
        "record": record,
        "trace": _read_truncated(entry_dir / "trace.txt", _MAX_TRACE_BYTES),
        "test_output": _read_truncated(entry_dir / "test_output.txt", _MAX_TRACE_BYTES),
        "flagged": (entry_dir / "FLAGGED").exists(),
    }


@mcp.tool()
def list_eval_runs(limit: int = 10) -> list[dict]:
    """List recent evaluation sweep runs (under evaluation/runs/, newest first).

    Each run is one `bench run` invocation. Returns run_id, agent names,
    fixture count, total record count, and fixed_count for quick scanning.
    """
    if not _RUNS.is_dir():
        return []

    out: list[dict] = []
    for run_dir in sorted(_RUNS.iterdir(), reverse=True):
        if not run_dir.is_dir():
            continue
        summary_path = run_dir / "summary.json"
        if not summary_path.exists():
            continue
        try:
            records = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(records, list):
            continue
        agents = sorted({r.get("agent_name", "?") for r in records if isinstance(r, dict)})
        fixtures = sorted({r.get("bug_id", "?") for r in records if isinstance(r, dict)})
        fixed = sum(1 for r in records if isinstance(r, dict) and r.get("outcome") == "fixed")
        out.append({
            "run_id": run_dir.name,
            "agents": agents,
            "fixture_count": len(fixtures),
            "record_count": len(records),
            "fixed_count": fixed,
        })
        if len(out) >= limit:
            break
    return out


@mcp.tool()
def promote_journal_to_fixture(
    entry_id: str,
    fixture_id: str,
    category: str = "unknown",
    difficulty: str = "medium",
    source_repo: str | None = None,
) -> dict:
    """Promote a journal entry into a curated fixture.

    Mirrors `python -m evaluation.cli promote <entry_id> --fixture-id <id>
    --category <c> --difficulty <d> [--source-repo <url-or-path>]`. The
    promote flow tries to populate source/ from the journal's recorded
    git commit; if it can't, the fixture is still created and source/
    needs manual population.

    Returns the exit code, stdout/stderr from the CLI, and whether the
    fixture directory now exists.
    """
    cmd = [
        "python", "-m", "evaluation.cli", "promote", entry_id,
        "--fixture-id", fixture_id,
        "--category", category,
        "--difficulty", difficulty,
    ]
    if source_repo:
        cmd.extend(["--source-repo", source_repo])

    result = subprocess.run(
        cmd, cwd=_ROOT, capture_output=True, text=True, timeout=120,
    )
    return {
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "fixture_created": (_FIXTURES / fixture_id).is_dir(),
    }


# ── Resources ────────────────────────────────────────────────────────────────

@mcp.resource("sdlcma://fixtures")
def fixtures_index() -> str:
    """Markdown index of all curated fixtures."""
    import sys
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    from evaluation.fixture import discover

    lines = ["# SDLCMA fixtures", ""]
    fixtures = discover()
    if not fixtures:
        lines.append("_(no fixtures found)_")
    for f in fixtures:
        notes = f.notes or "_(no notes)_"
        lines.append(f"- **{f.fixture_id}** ({f.category}, {f.difficulty}) — {notes}")
    return "\n".join(lines)


@mcp.resource("sdlcma://runs/{run_id}/summary")
def run_summary(run_id: str) -> str:
    """Raw summary.json for a single evaluation sweep run."""
    p = _RUNS / run_id / "summary.json"
    if not p.is_file():
        raise ValueError(f"no such run: {run_id}")
    return p.read_text(encoding="utf-8")


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
