"""max_input_tokens telemetry.

End-to-end wiring of the per-run "largest prompt the backend ever saw" metric:

  - react_loop tracks the running max across LLM calls and surfaces it in
    the returned state delta;
  - the running max is monotonic across react_loop re-entries (retries,
    acting-mode reviewer feedback) — a smaller later call must not lower it;
  - the reflection enhancement contributes to the same running max via its
    own (separate) LLM call;
  - RunRecord.from_outputs propagates the field from final_state to the
    canonical record consumed by the journal and evaluation sweep tooling.

The point of capturing this is operational: when running against a backend
with a finite context window (vLLM, on-prem), the largest prompt the system
emitted tells us how close we ran to the limit. Without this we only know
that runs failed, not whether they were starved for context.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from agents.run_record import RunRecord  # noqa: E402
from enhancements.reflection import make_reflection_callback  # noqa: E402


# ── fakes ─────────────────────────────────────────────────────────────────────


class _FakeMsg:
    """Minimal LangChain-shaped assistant message carrying usage_metadata.

    extract_token_usage prefers usage_metadata over response_metadata so this
    is the same path ChatOpenAI exercises in production.
    """

    def __init__(self, content: str, input_tokens: int, output_tokens: int = 5,
                 tool_calls=None):
        self.content = content
        self.usage_metadata = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        self.response_metadata = {}
        self.tool_calls = tool_calls or []


class _FakeLLM:
    def __init__(self, content: str = "WHY_IT_FAILED: ...\nNEXT_FOCUS: ...",
                 input_tokens: int = 100, output_tokens: int = 5):
        self._content = content
        self._in = input_tokens
        self._out = output_tokens

    def invoke(self, messages):
        return _FakeMsg(self._content, self._in, self._out)


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


# ── reflection: contributes to the running max ───────────────────────────────


def test_reflection_records_max_input_tokens():
    """First reflection call with no prior state → max equals this call's
    prompt_tokens."""
    reflect = make_reflection_callback(llm=_FakeLLM(input_tokens=321))
    out = reflect(_failed_state())
    assert out is not None
    assert out["max_input_tokens"] == 321


def test_reflection_keeps_prior_max_when_larger():
    """If react_loop has already pushed a bigger max into state, reflection
    must not lower it."""
    reflect = make_reflection_callback(llm=_FakeLLM(input_tokens=50))
    out = reflect(_failed_state(max_input_tokens=999))
    assert out["max_input_tokens"] == 999


def test_reflection_raises_max_when_its_call_is_larger():
    reflect = make_reflection_callback(llm=_FakeLLM(input_tokens=2048))
    out = reflect(_failed_state(max_input_tokens=512))
    assert out["max_input_tokens"] == 2048


# ── RunRecord: propagates the field from final_state ─────────────────────────


def test_run_record_from_outputs_pulls_max_input_tokens():
    rec = RunRecord.from_outputs(
        agent_name="LangGraphAgent",
        bug_id="BUG-1",
        outcome="fixed",
        error=None,
        iterations=0,
        final_state={"max_input_tokens": 8123},
    )
    assert rec.max_input_tokens == 8123


def test_run_record_from_outputs_defaults_to_none_when_missing():
    """Backward compat: a run that never recorded the field (e.g. pre-change
    journal entry round-trip) keeps None rather than crashing."""
    rec = RunRecord.from_outputs(
        agent_name="LangGraphAgent",
        bug_id="BUG-1",
        outcome="error",
        error="boom",
        iterations=0,
        final_state={},
    )
    assert rec.max_input_tokens is None


# ── react_loop: accumulates max across calls + carries forward on re-entry ──


def _stub_react_loop(monkeypatch, prompt_tokens_per_call: list[int]):
    """Replace react_loop's _invoke_llm_with_retry with a stub that returns a
    fake message with the next prompt_tokens value, then a submit_fix tool
    call so the loop terminates."""
    from graph.nodes import react_loop as rl

    calls = {"i": 0}

    def fake_invoke(messages):
        i = calls["i"]
        calls["i"] += 1
        in_tok = prompt_tokens_per_call[i]
        # Last call must terminate the loop via submit_fix.
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
            # A no-op nudge path: return text with no tool_calls, the loop
            # appends a nudge and loops back for the next stubbed call.
            tool_calls = []
        return _FakeMsg("ok", input_tokens=in_tok, tool_calls=tool_calls)

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
    # get_provider is required; the stubbed loop never actually calls it
    # because submit_fix is the first/last tool call.
    return {"configurable": {"provider": SimpleNamespace(), "hooks": None,
                             "budget": None}}


def test_react_loop_surfaces_max_across_calls(monkeypatch):
    rl = _stub_react_loop(monkeypatch, [123, 456, 250])
    out = rl.react_loop(_minimal_state(), _minimal_config())
    assert out["max_input_tokens"] == 456


def test_react_loop_carries_forward_prior_max(monkeypatch):
    """Re-entering react_loop (retry / acting-mode reviewer feedback) must
    not erase the max already accumulated by the previous pass."""
    rl = _stub_react_loop(monkeypatch, [100])
    state = _minimal_state()
    state["max_input_tokens"] = 9999  # prior pass already saw a bigger prompt
    out = rl.react_loop(state, _minimal_config())
    assert out["max_input_tokens"] == 9999


def test_react_loop_handles_backend_without_usage(monkeypatch):
    """Some backends (older OpenAI-compat servers) don't return usage at all.
    extract_token_usage returns 0 and the running max stays 0 — not None —
    so downstream tooling can distinguish "ran with no usage" from "no LLM
    call ever happened" (None)."""
    from graph.nodes import react_loop as rl

    def fake_invoke(messages):
        msg = SimpleNamespace(
            content="ok",
            usage_metadata=None,
            response_metadata={},
            tool_calls=[{
                "name": "abort_fix",
                "id": "call_1",
                "args": {"reason": "no idea"},
            }],
        )
        return msg

    monkeypatch.setattr(rl, "_invoke_llm_with_retry", fake_invoke)
    out = rl.react_loop(_minimal_state(), _minimal_config())
    assert out["max_input_tokens"] == 0
