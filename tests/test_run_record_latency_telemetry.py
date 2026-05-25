"""Per-run LLM latency / cost telemetry — wiring tests.

Mirrors test_run_record_max_input_tokens.py for the five additive fields:
llm_call_count, total_prompt_tokens, total_completion_tokens,
total_cached_input_tokens, total_llm_wallclock_s.

The point of these fields is operational: with self-hosted backends running
on a single GPU, knowing how much wallclock and how many tokens each agent ×
fixture cell burns is what makes "API vs self-hosted" / "baseline vs memory
vs reflection" actually comparable. Without them all we see is fix_rate.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from agents.run_record import RunRecord  # noqa: E402
from enhancements.reflection import make_reflection_callback  # noqa: E402
from services.budget import extract_cached_input_tokens  # noqa: E402


# ── fakes ─────────────────────────────────────────────────────────────────────


class _FakeMsg:
    """LangChain-shaped assistant message with usage_metadata (preferred path)
    and an optional cache-read detail. Sleeping briefly lets the perf_counter
    delta around the call be a real positive number we can assert > 0."""

    def __init__(self, content: str, input_tokens: int, output_tokens: int = 5,
                 cache_read: int | None = None, tool_calls=None):
        self.content = content
        meta: dict = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        if cache_read is not None:
            meta["input_token_details"] = {"cache_read": cache_read}
        self.usage_metadata = meta
        self.response_metadata = {}
        self.tool_calls = tool_calls or []


class _FakeLLM:
    def __init__(self, content: str = "WHY_IT_FAILED: ...\nNEXT_FOCUS: ...",
                 input_tokens: int = 100, output_tokens: int = 5,
                 cache_read: int | None = None, sleep_s: float = 0.005):
        self._content = content
        self._in = input_tokens
        self._out = output_tokens
        self._cache = cache_read
        self._sleep = sleep_s

    def invoke(self, messages):
        # A real backend takes time; sleep a touch so total_llm_wallclock_s is
        # an actual positive number we can assert on.
        time.sleep(self._sleep)
        return _FakeMsg(self._content, self._in, self._out, cache_read=self._cache)


def _failed_state(**overrides) -> dict:
    state = {
        "test_passed": False,
        "error_info": "AssertionError",
        "suspect_file_path": "calc.py",
        "llm_result": {"error_reason": "off-by-one", "fixes": []},
        "test_output": "FAILED",
    }
    state.update(overrides)
    return state


# ── extract_cached_input_tokens — both shapes + missing ──────────────────────


def test_extract_cached_from_langchain_input_token_details():
    msg = SimpleNamespace(
        usage_metadata={"input_tokens": 100, "input_token_details": {"cache_read": 40}},
        response_metadata={},
    )
    assert extract_cached_input_tokens(msg) == 40


def test_extract_cached_from_raw_openai_prompt_tokens_details():
    msg = SimpleNamespace(
        usage_metadata=None,
        response_metadata={
            "token_usage": {
                "prompt_tokens": 100,
                "prompt_tokens_details": {"cached_tokens": 25},
            }
        },
    )
    assert extract_cached_input_tokens(msg) == 25


def test_extract_cached_returns_none_when_backend_silent():
    """Most self-hosted backends never report prompt caching — distinct from
    'reported zero', which would return 0 instead of None."""
    msg = SimpleNamespace(
        usage_metadata={"input_tokens": 100, "output_tokens": 5},
        response_metadata={},
    )
    assert extract_cached_input_tokens(msg) is None


def test_extract_cached_returns_zero_when_backend_reports_zero():
    msg = SimpleNamespace(
        usage_metadata={"input_tokens": 100, "input_token_details": {"cache_read": 0}},
        response_metadata={},
    )
    assert extract_cached_input_tokens(msg) == 0


# ── reflection: contributes to latency totals ────────────────────────────────


def test_reflection_records_first_call_latency_and_tokens():
    reflect = make_reflection_callback(llm=_FakeLLM(input_tokens=321, output_tokens=12))
    out = reflect(_failed_state())
    assert out is not None
    assert out["llm_call_count"] == 1
    assert out["total_prompt_tokens"] == 321
    assert out["total_completion_tokens"] == 12
    assert out["total_llm_wallclock_s"] > 0
    # backend (the fake) did not report cache — stays None
    assert out["total_cached_input_tokens"] is None


def test_reflection_accumulates_onto_prior_totals():
    """When react_loop has already pushed totals into state, reflection adds to
    them rather than overwriting."""
    reflect = make_reflection_callback(llm=_FakeLLM(input_tokens=50, output_tokens=3))
    out = reflect(_failed_state(
        llm_call_count=2,
        total_prompt_tokens=1000,
        total_completion_tokens=120,
        total_llm_wallclock_s=4.5,
        total_cached_input_tokens=200,
    ))
    assert out["llm_call_count"] == 3
    assert out["total_prompt_tokens"] == 1050
    assert out["total_completion_tokens"] == 123
    assert out["total_llm_wallclock_s"] > 4.5
    # No cache_read on this call → prior cached count carries forward unchanged
    assert out["total_cached_input_tokens"] == 200


def test_reflection_with_cached_input_promotes_none_to_int():
    reflect = make_reflection_callback(
        llm=_FakeLLM(input_tokens=200, cache_read=80)
    )
    out = reflect(_failed_state())  # prior cached is None
    assert out["total_cached_input_tokens"] == 80


# ── RunRecord: propagates the 5 fields + llm_model_served ────────────────────


def test_run_record_pulls_all_latency_fields():
    rec = RunRecord.from_outputs(
        agent_name="LangGraphAgent",
        bug_id="BUG-1",
        outcome="fixed",
        error=None,
        iterations=0,
        final_state={
            "llm_call_count":            5,
            "total_prompt_tokens":       4321,
            "total_completion_tokens":   210,
            "total_cached_input_tokens": 1000,
            "total_llm_wallclock_s":     7.5,
        },
        llm_model_served="Qwen/Qwen2.5-Coder-32B-Instruct",
    )
    assert rec.llm_call_count == 5
    assert rec.total_prompt_tokens == 4321
    assert rec.total_completion_tokens == 210
    assert rec.total_cached_input_tokens == 1000
    assert rec.total_llm_wallclock_s == 7.5
    assert rec.llm_model_served == "Qwen/Qwen2.5-Coder-32B-Instruct"


def test_run_record_defaults_all_latency_fields_to_none():
    """Backward-compat: a pre-change journal entry round-trips with the new
    fields as None, never crashes."""
    rec = RunRecord.from_outputs(
        agent_name="LangGraphAgent",
        bug_id="BUG-1",
        outcome="error",
        error="boom",
        iterations=0,
        final_state={},
    )
    assert rec.llm_call_count is None
    assert rec.total_prompt_tokens is None
    assert rec.total_completion_tokens is None
    assert rec.total_cached_input_tokens is None
    assert rec.total_llm_wallclock_s is None
    assert rec.llm_model_served is None


# ── react_loop: accumulates totals across calls + carries forward ────────────


def _stub_react_loop(monkeypatch, prompt_tokens_per_call: list[int]):
    """Stub _invoke_llm_with_retry. Last call returns submit_fix to terminate.

    Sleeps 1ms per call so per-call wallclock is measurable.
    """
    from graph.nodes import react_loop as rl

    calls = {"i": 0}

    def fake_invoke(llm, messages):
        i = calls["i"]
        calls["i"] += 1
        in_tok = prompt_tokens_per_call[i]
        is_last = i == len(prompt_tokens_per_call) - 1
        if is_last:
            tool_calls = [{
                "name": "submit_fix",
                "id": "call_1",
                "args": {
                    "error_reason": "test",
                    "reasoning": "test",
                    "confidence": "high",
                    "fixes": [{
                        "file_path": "calc.py",
                        "line_number": 1,
                        "original_line": "a",
                        "new_line": "b",
                    }],
                },
            }]
        else:
            tool_calls = []
        time.sleep(0.002)
        return _FakeMsg("ok", input_tokens=in_tok, output_tokens=5, tool_calls=tool_calls)

    monkeypatch.setattr(rl, "_invoke_llm_with_retry", fake_invoke)
    return rl


def _minimal_state() -> dict:
    return {
        "bug_id": "BUG-1",
        "error_info": "trace",
        "suspect_file_path": "calc.py",
        "source_file_content": "x = 1\n",
        "parse_trace_fallback": False,
        "source_fetch_failed": False,
        "fix_retry_count": 0,
    }


def _minimal_config():
    return {"configurable": {"provider": SimpleNamespace(), "hooks": None,
                             "budget": None}}


def test_react_loop_accumulates_totals_across_calls(monkeypatch):
    rl = _stub_react_loop(monkeypatch, [100, 200, 150])
    out = rl.react_loop(_minimal_state(), _minimal_config())
    assert out["llm_call_count"] == 3
    assert out["total_prompt_tokens"] == 450
    assert out["total_completion_tokens"] == 15  # 5 per call × 3 calls
    assert out["total_llm_wallclock_s"] > 0
    assert out["total_cached_input_tokens"] is None  # fake didn't report cache


def test_react_loop_carries_forward_prior_totals(monkeypatch):
    """Re-entering react_loop (retry / acting-mode reviewer feedback) must add
    to the totals already accumulated, not reset them."""
    rl = _stub_react_loop(monkeypatch, [50])
    state = _minimal_state()
    state.update({
        "llm_call_count": 4,
        "total_prompt_tokens": 1000,
        "total_completion_tokens": 80,
        "total_llm_wallclock_s": 3.0,
        "total_cached_input_tokens": 400,
    })
    out = rl.react_loop(state, _minimal_config())
    assert out["llm_call_count"] == 5
    assert out["total_prompt_tokens"] == 1050
    assert out["total_completion_tokens"] == 85
    assert out["total_llm_wallclock_s"] > 3.0
    # No cache_read on this call — prior cached carries forward unchanged
    assert out["total_cached_input_tokens"] == 400


# ── metrics.aggregate handles mixed reporting ────────────────────────────────


def test_metrics_aggregate_handles_mixed_reporting(tmp_path, monkeypatch):
    """A cell with no LLM call (R10) plus a cell with full telemetry: the avg
    should be over the reporting cell only, not (value+0)/2."""
    import json
    from evaluation import metrics

    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(json.dumps([
        {"agent_name": "a", "outcome": "already_fixed", "iterations": 0,
         "elapsed_s": 0.1, "total_prompt_tokens": None,
         "total_completion_tokens": None, "total_llm_wallclock_s": None,
         "matches_expected": True},
        {"agent_name": "a", "outcome": "fixed", "iterations": 1,
         "elapsed_s": 5.0, "total_prompt_tokens": 1000,
         "total_completion_tokens": 100, "total_llm_wallclock_s": 4.0,
         "matches_expected": True},
    ]))
    monkeypatch.setattr(metrics, "_RUNS_ROOT", tmp_path / "runs")
    rows = metrics.aggregate("r1")
    assert len(rows) == 1
    r = rows[0]
    # Mean is over the ONE reporting cell — 1100, not 550.
    assert r["avg_total_tokens"] == 1100.0
    assert r["avg_llm_wallclock_s"] == 4.0
    assert r["tokens_per_s"] == 275.0  # 1100 / 4
