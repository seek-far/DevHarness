"""Unit tests for mcp_server.server.

FastMCP's @tool / @resource decorators preserve the underlying function, so
we test them as plain Python — no MCP transport, no client harness needed.
The tests run against the repo's real evaluation/{fixtures,journal,runs}/
data; if those directories are present (they're tracked) the tests assert
on stable shapes, not concrete content.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_server.server import (  # noqa: E402
    list_fixtures,
    read_fixture,
    list_journal_entries,
    read_journal_entry,
    list_eval_runs,
    fixtures_index,
    run_summary,
)


# ── list_fixtures ────────────────────────────────────────────────────────────

def test_list_fixtures_returns_known_shape():
    fixtures = list_fixtures()
    assert isinstance(fixtures, list)
    assert len(fixtures) > 0, "evaluation/fixtures/ should contain F01..Fn"
    f0 = fixtures[0]
    # Stable shape — these keys are the contract MCP clients depend on.
    for key in ("fixture_id", "category", "difficulty", "expected_outcome",
                "has_trace", "has_expected_patch", "notes"):
        assert key in f0, f"missing key {key} in {f0}"


def test_list_fixtures_includes_F01():
    fixtures = list_fixtures()
    ids = [f["fixture_id"] for f in fixtures]
    assert "F01-off-by-one" in ids


# ── read_fixture ─────────────────────────────────────────────────────────────

def test_read_fixture_known_id():
    f = read_fixture("F01-off-by-one")
    assert f["fixture_id"] == "F01-off-by-one"
    assert f["meta"]["category"] == "off-by-one"
    assert isinstance(f["source"], dict)
    assert len(f["source"]) > 0
    # source values are strings (truncated content, not binary).
    for path, content in f["source"].items():
        assert isinstance(path, str)
        assert isinstance(content, str)


def test_read_fixture_nonexistent_raises():
    with pytest.raises(ValueError, match="no such fixture"):
        read_fixture("F999-nonexistent")


# ── list_journal_entries ─────────────────────────────────────────────────────

def test_list_journal_entries_default_limit():
    entries = list_journal_entries()
    # Repo's evaluation/journal/ is non-empty in the working tree.
    assert isinstance(entries, list)
    if entries:
        for e in entries:
            for key in ("entry_id", "flagged", "outcome", "iterations",
                        "timestamp", "bug_id"):
                assert key in e


def test_list_journal_entries_limit_respected():
    entries = list_journal_entries(limit=2)
    assert len(entries) <= 2


def test_list_journal_entries_flagged_filter():
    flagged = list_journal_entries(flagged_only=True, limit=50)
    for e in flagged:
        assert e["flagged"] is True


# ── read_journal_entry ───────────────────────────────────────────────────────

def test_read_journal_entry_round_trip():
    entries = list_journal_entries(limit=1)
    if not entries:
        pytest.skip("journal empty — cannot round-trip")
    entry_id = entries[0]["entry_id"]

    je = read_journal_entry(entry_id)
    assert je["entry_id"] == entry_id
    assert isinstance(je["record"], dict)
    # outcome must be one of the documented values when present
    assert je["record"].get("outcome") in (None, "fixed", "no_fix", "error")


def test_read_journal_entry_nonexistent_raises():
    with pytest.raises(ValueError, match="no such journal entry"):
        read_journal_entry("nope-not-a-real-entry")


# ── list_eval_runs ───────────────────────────────────────────────────────────

def test_list_eval_runs_shape():
    runs = list_eval_runs(limit=5)
    assert isinstance(runs, list)
    for r in runs:
        for key in ("run_id", "agents", "fixture_count", "record_count",
                    "fixed_count"):
            assert key in r
        assert isinstance(r["agents"], list)
        assert r["fixed_count"] <= r["record_count"]


# ── resources ────────────────────────────────────────────────────────────────

def test_fixtures_index_resource_is_markdown():
    out = fixtures_index()
    assert out.startswith("# SDLCMA fixtures")
    # Should mention at least F01 by id.
    assert "F01-off-by-one" in out


def test_run_summary_resource_known_id():
    runs = list_eval_runs(limit=1)
    if not runs:
        pytest.skip("no eval runs available")
    s = run_summary(runs[0]["run_id"])
    # Raw JSON text — re-parseable.
    import json
    parsed = json.loads(s)
    assert isinstance(parsed, list)


def test_run_summary_resource_unknown_raises():
    with pytest.raises(ValueError, match="no such run"):
        run_summary("run_does_not_exist")
