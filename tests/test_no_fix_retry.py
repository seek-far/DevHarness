"""no_fix retry — react_loop self-loop when LLM exits without submit_fix.

Behaviour under test:

  1. react_loop bumps `no_fix_retry_count` on every llm_result=None exit
     (MAX_STEPS reached, or abort_fix called). Unconditional — the gating
     happens in the router.
  2. `route_after_react_loop` returns "react_loop" (self-loop) when:
        - llm_result is None, AND
        - cfg.llm_via_gateway is True, AND
        - state.no_fix_retry_count <= NO_FIX_MAX_RETRIES
     Otherwise returns "handle_failure" on the no-fix path (existing
     behaviour). Gateway-off mode = byte-identical to pre-feature.
  3. The combined gateway attempt header on the next react_loop entry is
     `fix_retry_count + no_fix_retry_count` so the policy sees a single
     "I have failed N times" signal regardless of failure mode.

These tests stub `_invoke_llm_with_retry` so no real LLM is needed.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bf_worker"))


# ── router ────────────────────────────────────────────────────────────────────


def test_router_no_fix_gateway_off_handle_failure(monkeypatch):
    """Gateway off → no-fix exit goes straight to handle_failure (legacy)."""
    from graph import routing

    monkeypatch.setattr(routing.cfg, "llm_via_gateway", False, raising=False)
    state = {"llm_result": None, "no_fix_retry_count": 1}
    assert routing.route_after_react_loop(state) == "handle_failure"


def test_router_no_fix_gateway_on_first_exit_retries(monkeypatch):
    """Gateway on + first no-fix exit (counter=1) → re-enter react_loop."""
    from graph import routing

    monkeypatch.setattr(routing.cfg, "llm_via_gateway", True, raising=False)
    # react_loop just bumped 0→1 and returned.
    state = {"llm_result": None, "no_fix_retry_count": 1}
    assert routing.route_after_react_loop(state) == "react_loop"


def test_router_no_fix_gateway_on_cap_reached_fails(monkeypatch):
    """Gateway on + counter past NO_FIX_MAX_RETRIES → handle_failure."""
    from graph import routing

    monkeypatch.setattr(routing.cfg, "llm_via_gateway", True, raising=False)
    state = {"llm_result": None, "no_fix_retry_count": routing.NO_FIX_MAX_RETRIES + 1}
    assert routing.route_after_react_loop(state) == "handle_failure"


def test_router_no_fix_at_exact_cap_still_retries(monkeypatch):
    """Boundary: counter == NO_FIX_MAX_RETRIES is still "≤" → loop. Becomes
    "> cap" only on the NEXT exit."""
    from graph import routing

    monkeypatch.setattr(routing.cfg, "llm_via_gateway", True, raising=False)
    state = {"llm_result": None, "no_fix_retry_count": routing.NO_FIX_MAX_RETRIES}
    assert routing.route_after_react_loop(state) == "react_loop"


def test_router_llm_result_present_path_unchanged(monkeypatch):
    """When llm_result is set, no_fix_retry_count is irrelevant — normal
    forward routing applies. Sanity that we didn't break the happy path."""
    from graph import routing

    monkeypatch.setattr(routing.cfg, "llm_via_gateway", True, raising=False)
    state = {"llm_result": {"fixes": [{"file_path": "x"}]}, "no_fix_retry_count": 5}
    assert routing.route_after_react_loop(state) == "create_fix_branch"


# ── react_loop bumps the counter on the no-fix exit path ──────────────────────


def _abort_fix_msg():
    return SimpleNamespace(
        content="ok",
        usage_metadata=None,
        response_metadata={},
        tool_calls=[{
            "name": "abort_fix",
            "id": "call_1",
            "args": {"reason": "no idea"},
        }],
    )


def _minimal_state(no_fix_count: int = 0) -> dict:
    return {
        "bug_id": "BUG-NF-1",
        "error_info": "trace",
        "suspect_file_path": "calc.py",
        "source_file_content": "x = 1\n",
        "parse_trace_fallback": False,
        "source_fetch_failed": False,
        "fix_retry_count": 0,
        "no_fix_retry_count": no_fix_count,
    }


def _minimal_config():
    return {"configurable": {"provider": SimpleNamespace(), "hooks": None,
                             "budget": None}}


def test_react_loop_bumps_counter_on_abort_fix(monkeypatch):
    """abort_fix → llm_result=None → no_fix_retry_count bumped by 1."""
    from graph.nodes import react_loop as rl

    monkeypatch.setattr(rl, "_invoke_llm_with_retry", lambda llm, msgs: _abort_fix_msg())
    out = rl.react_loop(_minimal_state(no_fix_count=0), _minimal_config())
    assert out["llm_result"] is None
    assert out["no_fix_retry_count"] == 1


def test_react_loop_bumps_counter_compounding(monkeypatch):
    """Counter increments from whatever the input was — react_loop doesn't
    reset, it accumulates across re-entries."""
    from graph.nodes import react_loop as rl

    monkeypatch.setattr(rl, "_invoke_llm_with_retry", lambda llm, msgs: _abort_fix_msg())
    out = rl.react_loop(_minimal_state(no_fix_count=1), _minimal_config())
    assert out["no_fix_retry_count"] == 2


def test_react_loop_does_not_bump_on_submit_fix(monkeypatch):
    """submit_fix → llm_result is set → counter unchanged."""
    from graph.nodes import react_loop as rl

    def fake(llm, msgs):
        return SimpleNamespace(
            content="ok",
            usage_metadata=None,
            response_metadata={},
            tool_calls=[{
                "name": "submit_fix",
                "id": "call_1",
                "args": {
                    "error_reason": "test",
                    "reasoning": "test",
                    "confidence": "high",
                    "fixes": [{
                        "file_path": "calc.py",
                        "line_number": 1,
                        "original_line": "x = 1",
                        "new_line": "x = 2",
                    }],
                },
            }],
        )

    monkeypatch.setattr(rl, "_invoke_llm_with_retry", fake)
    out = rl.react_loop(_minimal_state(no_fix_count=3), _minimal_config())
    assert out["llm_result"] is not None
    assert out["no_fix_retry_count"] == 3  # unchanged


# ── attempt header reflects the SUM of both retry counters ────────────────────


def test_react_loop_attempt_header_sums_both_counters(monkeypatch):
    """When the worker is in gateway mode, the X-Sdlcma-Attempt header sent
    to the gateway is `fix_retry_count + no_fix_retry_count`. We can't
    inspect the HTTP header without a real network round-trip, so we
    inspect the ChatOpenAI client built by build_llm_with_headers — its
    default_headers reflect the value that would be sent."""
    from graph.nodes import react_loop as rl
    from settings import worker_cfg as cfg

    monkeypatch.setattr(cfg, "llm_via_gateway", True, raising=False)
    captured = {}

    def fake_build(*, bug_id, attempt, tools):
        captured["bug_id"] = bug_id
        captured["attempt"] = attempt
        # Return a stub llm whose .invoke returns an abort msg so react_loop
        # exits cleanly (we only care about the build call's args).
        class _Stub:
            def invoke(self, _msgs):
                return _abort_fix_msg()
        return _Stub()

    monkeypatch.setattr(rl, "build_llm_with_headers", fake_build)
    state = _minimal_state(no_fix_count=1)
    state["fix_retry_count"] = 2
    rl.react_loop(state, _minimal_config())
    assert captured["attempt"] == 3  # 2 + 1
    assert captured["bug_id"] == "BUG-NF-1"


def test_react_loop_attempt_header_zero_when_both_counters_zero(monkeypatch):
    """Initial entry: both counters 0 → gateway sees attempt=0 → primary."""
    from graph.nodes import react_loop as rl
    from settings import worker_cfg as cfg

    monkeypatch.setattr(cfg, "llm_via_gateway", True, raising=False)
    captured = {}

    def fake_build(*, bug_id, attempt, tools):
        captured["attempt"] = attempt
        class _Stub:
            def invoke(self, _msgs):
                return _abort_fix_msg()
        return _Stub()

    monkeypatch.setattr(rl, "build_llm_with_headers", fake_build)
    rl.react_loop(_minimal_state(no_fix_count=0), _minimal_config())
    assert captured["attempt"] == 0
