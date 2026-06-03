"""tools/runrecord_to_metrics.py — scan + aggregate + render tests.

Covers the three responsibilities of the script:
  1. _scan_journal walks `record.json` files, tolerates malformed.
  2. _aggregate sums by the right buckets (agent / model_slug /
     outcome / token-type), respects None / missing fields, and
     keeps cardinality bounded via slugify_model.
  3. render produces valid Prometheus exposition format (HELP / TYPE
     lines per family, escaped label values).

Plus an end-to-end main() smoke that walks a tmp journal and writes
the output file atomically.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.runrecord_to_metrics import (
    _aggregate,
    _scan_journal,
    main,
    render,
    slugify_model,
)


def _write_record(parent: Path, name: str, record: dict) -> None:
    d = parent / name
    d.mkdir()
    (d / "record.json").write_text(json.dumps(record), encoding="utf-8")


def _make_record(**overrides) -> dict:
    """Minimal RunRecord with the fields runrecord_to_metrics reads."""
    base = {
        "agent_name": "langgraph",
        "llm_model": "qwen3-coder-480b-a35b-instruct",
        "outcome": "fixed",
        "elapsed_s": 60.0,
        "total_prompt_tokens": 1000,
        "total_completion_tokens": 100,
        "total_cached_input_tokens": None,
        "llm_call_count": 3,
        "parse_trace_fallback": False,
        "reflection_count": None,
    }
    base.update(overrides)
    return base


# ── slugify_model ───────────────────────────────────────────────────────────


def test_slugify_replaces_slashes_with_dashes():
    """Slashes are the most common high-cardinality leak (Anthropic-style
    model names include `/`). Replace them so Prometheus stores a sane
    label string."""
    assert slugify_model("anthropic/claude-opus-4-7") == "anthropic-claude-opus-4-7"


def test_slugify_caps_at_60_chars():
    """A misconfigured llm_model containing a URL or path could blow
    up the model_slug label. Cap prevents that."""
    long_name = "x" * 200
    assert len(slugify_model(long_name)) == 60


def test_slugify_handles_none_and_empty():
    assert slugify_model(None) == "unknown"
    assert slugify_model("") == "unknown"


# ── _scan_journal ─────────────────────────────────────────────────────────-


def test_scan_finds_all_record_json_files(tmp_path):
    _write_record(tmp_path, "20260520T120000Z_BUG-1_langgraph", _make_record())
    _write_record(tmp_path, "20260521T120000Z_BUG-2_langgraph", _make_record())
    # Sibling that isn't a journal dir (no record.json) — must be skipped.
    (tmp_path / "loose.txt").write_text("ignore me")
    records = _scan_journal(tmp_path)
    assert len(records) == 2


def test_scan_tolerates_malformed_json(tmp_path, caplog):
    """A half-written record.json (cron running while a fix is in
    progress) shouldn't crash the scrape. Log a warning, continue."""
    import logging
    caplog.set_level(logging.WARNING, logger="runrecord_to_metrics")
    _write_record(tmp_path, "good_dir", _make_record())
    bad = tmp_path / "bad_dir"
    bad.mkdir()
    (bad / "record.json").write_text("{ this is not json", encoding="utf-8")
    records = _scan_journal(tmp_path)
    assert len(records) == 1   # only the good one
    assert any("skipping" in r.message for r in caplog.records)


def test_scan_missing_journal_dir_returns_empty(tmp_path):
    """Brand-new install — `evaluation/journal/` may not exist yet.
    Returns empty and doesn't crash; the metrics file should still be
    written (all counters at 0)."""
    records = _scan_journal(tmp_path / "no_such_dir")
    assert records == []


# ── _aggregate ─────────────────────────────────────────────────────────────


def test_aggregate_counts_fixes_per_outcome():
    records = [
        _make_record(outcome="fixed"),
        _make_record(outcome="fixed"),
        _make_record(outcome="error"),
    ]
    agg = _aggregate(records)
    fixes = dict(agg["fixes"])
    key_fixed = ("langgraph", "qwen3-coder-480b-a35b-instruct", "fixed")
    key_error = ("langgraph", "qwen3-coder-480b-a35b-instruct", "error")
    assert fixes[key_fixed] == 2
    assert fixes[key_error] == 1


def test_aggregate_sums_elapsed():
    records = [
        _make_record(elapsed_s=30.0),
        _make_record(elapsed_s=60.0),
    ]
    agg = _aggregate(records)
    elapsed = dict(agg["elapsed"])
    key = ("langgraph", "qwen3-coder-480b-a35b-instruct", "fixed")
    assert elapsed[key] == 90.0


def test_aggregate_sums_tokens_by_type():
    records = [
        _make_record(total_prompt_tokens=1000, total_completion_tokens=100),
        _make_record(total_prompt_tokens=500,  total_completion_tokens=50),
    ]
    agg = _aggregate(records)
    tokens = dict(agg["tokens"])
    model_slug = "qwen3-coder-480b-a35b-instruct"
    assert tokens[("prompt", model_slug)] == 1500
    assert tokens[("completion", model_slug)] == 150
    # cached was None on both → not counted
    assert ("cached", model_slug) not in tokens


def test_aggregate_handles_none_fields_gracefully():
    """A pre-schema-bump RunRecord may have None for any new field.
    Must NOT crash — those records just contribute 0 to the
    accumulator."""
    records = [
        _make_record(elapsed_s=None, total_prompt_tokens=None,
                     llm_call_count=None, reflection_count=None),
        _make_record(),  # fully populated
    ]
    agg = _aggregate(records)
    # Both records counted (runs_total always increments, success or not).
    fixes = dict(agg["fixes"])
    key = ("langgraph", "qwen3-coder-480b-a35b-instruct", "fixed")
    assert fixes[key] == 2
    # Only the populated record contributes to elapsed/tokens.
    assert dict(agg["elapsed"])[key] == 60.0
    assert dict(agg["tokens"])[("prompt", "qwen3-coder-480b-a35b-instruct")] == 1000


def test_aggregate_parse_trace_fallback_counts_only_true():
    """The metric is a counter of `True` values, not all records.
    A 100% fallback rate flagged the parser bug live 2026-05-29 — pin
    that we only increment on True."""
    records = [
        _make_record(parse_trace_fallback=True),
        _make_record(parse_trace_fallback=True),
        _make_record(parse_trace_fallback=False),
        _make_record(parse_trace_fallback=None),
    ]
    agg = _aggregate(records)
    assert dict(agg["parse_fb"])["langgraph"] == 2


def test_aggregate_reflection_counts_records_with_at_least_one_fire():
    """Reflection enhancement might fire multiple times per fix;
    metric is "number of fixes where reflection fired at least once",
    not the sum of fires. Tells you A/B-test impact directly."""
    records = [
        _make_record(reflection_count=1),
        _make_record(reflection_count=3),
        _make_record(reflection_count=0),    # didn't fire
        _make_record(reflection_count=None), # not configured
    ]
    agg = _aggregate(records)
    assert dict(agg["reflection"])["langgraph"] == 2


def test_aggregate_different_agents_separated():
    """Multi-agent eval sweeps must keep buckets distinct so
    fix_rate-by-agent is recoverable."""
    records = [
        _make_record(agent_name="baseline"),
        _make_record(agent_name="reflection"),
        _make_record(agent_name="baseline"),
    ]
    agg = _aggregate(records)
    fixes = dict(agg["fixes"])
    key_baseline = ("baseline", "qwen3-coder-480b-a35b-instruct", "fixed")
    key_reflection = ("reflection", "qwen3-coder-480b-a35b-instruct", "fixed")
    assert fixes[key_baseline] == 2
    assert fixes[key_reflection] == 1


# ── render ─────────────────────────────────────────────────────────────────


def test_render_produces_valid_prometheus_format():
    records = [_make_record(outcome="fixed", elapsed_s=60.0)]
    agg = _aggregate(records)
    body = render(agg, scan_ts=1234567890.0)
    # Every metric family has HELP + TYPE.
    assert "# HELP sdlcma_runs_total " in body
    assert "# TYPE sdlcma_runs_total counter" in body
    assert "# HELP sdlcma_runrecord_last_scan_timestamp " in body
    assert "# TYPE sdlcma_runrecord_last_scan_timestamp gauge" in body
    # The actual sample line carries the label set in the right shape.
    assert (
        'sdlcma_runs_total{agent="langgraph",'
        'model_slug="qwen3-coder-480b-a35b-instruct",outcome="fixed"} 1'
    ) in body
    # Gauge has no labels.
    assert "sdlcma_runrecord_last_scan_timestamp 1234567890.0" in body


def test_render_escapes_special_chars_in_labels():
    """A model slug containing characters that survive slugify (e.g.
    underscore/dot) must still render — slugify already trims the
    really nasty ones, this is a defensive belt."""
    agg = _aggregate([_make_record(llm_model="my.model_v2")])
    body = render(agg, scan_ts=0.0)
    assert "my.model_v2" in body


# ── main() end-to-end ──────────────────────────────────────────────────────


def test_main_writes_output_file_atomically(tmp_path):
    journal = tmp_path / "journal"
    journal.mkdir()
    _write_record(journal, "20260520T120000Z_BUG-1_langgraph", _make_record())
    out_path = tmp_path / "out" / "sdlcma_runrecord.prom"

    rc = main([
        "--journal-dir", str(journal),
        "--out", str(out_path),
    ])
    assert rc == 0
    assert out_path.is_file()
    body = out_path.read_text(encoding="utf-8")
    assert "sdlcma_runs_total" in body
    # No leftover .tmp file (atomic rename succeeded).
    assert not out_path.with_suffix(out_path.suffix + ".tmp").exists()


def test_main_empty_journal_still_writes_a_file(tmp_path):
    """A brand-new install with no fixes yet shouldn't break the
    scrape — output file exists with counter families at 0 and a
    fresh scan_timestamp."""
    rc = main([
        "--journal-dir", str(tmp_path / "empty_journal"),
        "--out", str(tmp_path / "sdlcma_runrecord.prom"),
    ])
    assert rc == 0
    body = (tmp_path / "sdlcma_runrecord.prom").read_text()
    assert "sdlcma_runs_total" in body
    assert "sdlcma_runrecord_last_scan_timestamp" in body
