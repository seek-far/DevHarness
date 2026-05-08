"""Tests for evaluation/journal_prune.py.

Covers the planning function (pure, deterministic via injected `now`) and the
executor (rmtree behavior + dry-run guarantee). Also covers parse_duration
because typo'd retention windows must surface as errors, not as zero.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.journal_prune import (  # noqa: E402
    parse_duration,
    plan_prune,
    run_prune,
    _entry_timestamp,
)


# Reference point for deterministic time-based assertions. All test entries
# are placed on a timeline relative to this `now`.
NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)


def _make_entry(journal_dir: Path, name: str, *, flagged: bool = False) -> Path:
    """Create a fake journal entry directory matching the real layout."""
    entry = journal_dir / name
    entry.mkdir()
    (entry / "record.json").write_text("{}", encoding="utf-8")
    if flagged:
        (entry / "FLAGGED").write_text("test\n", encoding="utf-8")
    return entry


def _name_at(offset_days: float, *, bug_id: str = "BUG-1", agent: str = "langgraph",
             slug: str = "") -> str:
    ts = (NOW - timedelta(days=offset_days)).strftime("%Y%m%dT%H%M%SZ")
    base = f"{ts}_{bug_id}_{agent}"
    return f"{base}_{slug}" if slug else base


# ── parse_duration ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "s, expected",
    [
        ("30d", timedelta(days=30)),
        ("1d",  timedelta(days=1)),
        ("12h", timedelta(hours=12)),
        ("90m", timedelta(minutes=90)),
        ("60s", timedelta(seconds=60)),
    ],
)
def test_parse_duration_valid(s, expected):
    assert parse_duration(s) == expected


@pytest.mark.parametrize(
    "bad",
    ["", "30", "30D", "1d12h", "0.5d", "thirty days", "-1d", "30 d"],
)
def test_parse_duration_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


# ── _entry_timestamp ─────────────────────────────────────────────────────────


def test_entry_timestamp_parses_canonical_name():
    ts = _entry_timestamp("20260503T120000Z_BUG-1_langgraph")
    assert ts == datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc)


def test_entry_timestamp_parses_name_with_model_slug():
    ts = _entry_timestamp("20260503T120000Z_BUG-1_langgraph_qwen-coder-plus")
    assert ts == datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "name",
    [
        "README.md",
        "notes",
        "20260503_BUG_no-Z",            # missing T...Z shape
        "abc-not-a-timestamp_BUG-1",
        "20269999T999999Z_BUG-1",       # parseable shape but invalid date
    ],
)
def test_entry_timestamp_returns_none_for_unrecognized(name):
    assert _entry_timestamp(name) is None


# ── plan_prune ───────────────────────────────────────────────────────────────


def test_plan_keeps_unrecognized_names(tmp_path):
    (tmp_path / "README.md").write_text("hands off\n", encoding="utf-8")
    (tmp_path / "manual_notes").mkdir()
    plan = plan_prune(tmp_path, older_than=timedelta(days=1),
                      keep_flagged=False, now=NOW)
    # README.md is a file at the top level — not even iterated as a candidate.
    # manual_notes is a directory whose name doesn't match — explicitly kept.
    kept_names = {p.name for p, _ in plan.to_keep}
    assert "manual_notes" in kept_names
    assert plan.to_delete == []


def test_plan_keeps_within_window(tmp_path):
    fresh = _make_entry(tmp_path, _name_at(0.5))  # 12h ago
    plan = plan_prune(tmp_path, older_than=timedelta(days=1),
                      keep_flagged=False, now=NOW)
    assert plan.to_delete == []
    assert any(p == fresh for p, _ in plan.to_keep)


def test_plan_deletes_outside_window_when_not_flagged(tmp_path):
    old = _make_entry(tmp_path, _name_at(40))  # 40 days ago, not flagged
    plan = plan_prune(tmp_path, older_than=timedelta(days=30),
                      keep_flagged=False, now=NOW)
    assert plan.to_delete == [old]


def test_plan_keep_flagged_true_protects_old_flagged(tmp_path):
    old_flagged = _make_entry(tmp_path, _name_at(40, bug_id="BUG-A"),
                              flagged=True)
    old_plain   = _make_entry(tmp_path, _name_at(40, bug_id="BUG-B"))
    plan = plan_prune(tmp_path, older_than=timedelta(days=30),
                      keep_flagged=True, now=NOW)
    assert old_plain in plan.to_delete
    assert old_flagged not in plan.to_delete
    # And the kept-list reason is the FLAGGED protection, not "within window"
    reasons = {p.name: why for p, why in plan.to_keep}
    assert reasons[old_flagged.name] == "FLAGGED"


def test_plan_keep_flagged_false_deletes_old_flagged(tmp_path):
    old_flagged = _make_entry(tmp_path, _name_at(40), flagged=True)
    plan = plan_prune(tmp_path, older_than=timedelta(days=30),
                      keep_flagged=False, now=NOW)
    # When the operator explicitly says they don't want to keep flagged, we honor it.
    assert old_flagged in plan.to_delete


def test_plan_returns_empty_when_journal_dir_missing(tmp_path):
    missing = tmp_path / "does-not-exist"
    plan = plan_prune(missing, older_than=timedelta(days=1),
                      keep_flagged=True, now=NOW)
    assert plan.to_delete == [] and plan.to_keep == []


# ── run_prune ────────────────────────────────────────────────────────────────


def test_run_prune_dry_run_does_not_touch_disk(tmp_path):
    old = _make_entry(tmp_path, _name_at(40))
    summary = run_prune(tmp_path, older_than=timedelta(days=30),
                        keep_flagged=False, apply=False, now=NOW)
    assert summary["dry_run"] is True
    assert summary["candidates"] == [old.name]
    assert summary["deleted"] == []
    # Disk untouched.
    assert old.exists()


def test_run_prune_apply_actually_deletes(tmp_path):
    old   = _make_entry(tmp_path, _name_at(40))
    fresh = _make_entry(tmp_path, _name_at(0.5))
    summary = run_prune(tmp_path, older_than=timedelta(days=30),
                        keep_flagged=False, apply=True, now=NOW)
    assert summary["dry_run"] is False
    assert summary["deleted"] == [old.name]
    assert not old.exists()
    assert fresh.exists()


def test_run_prune_apply_skips_flagged_when_requested(tmp_path):
    old_flagged = _make_entry(tmp_path, _name_at(40), flagged=True)
    summary = run_prune(tmp_path, older_than=timedelta(days=30),
                        keep_flagged=True, apply=True, now=NOW)
    assert summary["deleted"] == []
    assert old_flagged.exists()
    assert summary["candidates"] == []
