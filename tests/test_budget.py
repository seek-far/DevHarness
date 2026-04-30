"""Unit tests for the per-run cost budget."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from services.budget import (  # noqa: E402
    DEFAULT_MAX_CALLS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MAX_WALLCLOCK_S,
    RunBudget,
    extract_token_usage,
)


# ── construction ──────────────────────────────────────────────────────────────

def test_defaults_are_what_docs_advertise() -> None:
    b = RunBudget()
    assert b.max_calls == DEFAULT_MAX_CALLS == 30
    assert b.max_tokens == DEFAULT_MAX_TOKENS == 200_000
    assert b.max_wallclock_s == DEFAULT_MAX_WALLCLOCK_S == 300


def test_fresh_budget_is_not_exhausted() -> None:
    b = RunBudget()
    assert b.check() is None
    assert not b.is_exhausted()


# ── debit / readers ──────────────────────────────────────────────────────────

def test_record_call_increments_counters() -> None:
    b = RunBudget()
    b.record_call(input_tokens=120, output_tokens=40)
    b.record_call(input_tokens=200, output_tokens=10)
    assert b.calls == 2
    assert b.input_tokens == 320
    assert b.output_tokens == 50
    assert b.total_tokens == 370


def test_record_call_handles_missing_usage() -> None:
    b = RunBudget()
    b.record_call()                    # no token info at all
    b.record_call(input_tokens=None, output_tokens=None)  # type: ignore[arg-type]
    assert b.calls == 2
    assert b.total_tokens == 0


def test_negative_token_counts_are_clamped() -> None:
    b = RunBudget()
    b.record_call(input_tokens=-5, output_tokens=-10)
    assert b.input_tokens == 0
    assert b.output_tokens == 0


# ── exhaustion ────────────────────────────────────────────────────────────────

def test_call_cap_trips() -> None:
    b = RunBudget(max_calls=2)
    b.record_call(1, 1)
    assert b.check() is None
    b.record_call(1, 1)
    reason = b.check()
    assert reason is not None
    assert "call limit" in reason
    assert b.is_exhausted()


def test_token_cap_trips_first_when_relevant() -> None:
    b = RunBudget(max_calls=10, max_tokens=100)
    b.record_call(input_tokens=60, output_tokens=50)  # 110 > 100
    reason = b.check()
    assert reason is not None
    assert "token limit" in reason


def test_wallclock_cap_trips() -> None:
    b = RunBudget(max_wallclock_s=0)
    # max_wallclock_s=0 means any elapsed time exhausts; the budget started
    # in __init__ so even one call's worth of monotonic time is enough.
    time.sleep(0.001)
    reason = b.check()
    assert reason is not None
    assert "wallclock limit" in reason


def test_first_exhaustion_reason_is_sticky() -> None:
    """Once a reason latches, later check() calls return the same reason
    even when other caps cross — useful for run-record fidelity.
    """
    b = RunBudget(max_calls=1, max_tokens=10)
    b.record_call(1, 1)
    first = b.check()
    assert first is not None and "call limit" in first
    # Now blow through tokens too. The recorded reason should still be the
    # first one that tripped, not a later one.
    b.input_tokens = 999_999
    assert b.check() == first


# ── serialisation ────────────────────────────────────────────────────────────

def test_to_dict_snapshot_is_serialisable_and_complete() -> None:
    import json

    b = RunBudget(max_calls=5)
    b.record_call(input_tokens=100, output_tokens=50)
    snap = b.to_dict()
    json.dumps(snap)  # round-trips cleanly
    for key in (
        "max_calls", "max_tokens", "max_wallclock_s",
        "calls", "input_tokens", "output_tokens", "total_tokens",
        "elapsed_s", "exhausted_reason",
    ):
        assert key in snap
    assert snap["calls"] == 1
    assert snap["total_tokens"] == 150
    assert snap["exhausted_reason"] is None


# ── extract_token_usage ──────────────────────────────────────────────────────

def test_extract_prefers_usage_metadata() -> None:
    msg = SimpleNamespace(
        usage_metadata={"input_tokens": 123, "output_tokens": 45},
        response_metadata={"token_usage": {"prompt_tokens": 999, "completion_tokens": 999}},
    )
    assert extract_token_usage(msg) == (123, 45)


def test_extract_falls_back_to_response_metadata() -> None:
    msg = SimpleNamespace(
        usage_metadata=None,
        response_metadata={"token_usage": {"prompt_tokens": 200, "completion_tokens": 30}},
    )
    assert extract_token_usage(msg) == (200, 30)


def test_extract_returns_zeros_when_nothing_available() -> None:
    msg = SimpleNamespace()
    assert extract_token_usage(msg) == (0, 0)
    msg = SimpleNamespace(usage_metadata=None, response_metadata={})
    assert extract_token_usage(msg) == (0, 0)
