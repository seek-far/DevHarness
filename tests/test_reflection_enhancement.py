"""Reflection enhancement (POST_APPLY_TEST).

Covers:
  - the callback turns a failed apply+test state into a structured
    reflection_note (+ reflection_count), via an injected fake LLM (no net);
  - it is a strict no-op on a passing run;
  - it respects the run budget (skips when exhausted, records when it runs);
  - an LLM failure is swallowed (never breaks the core run);
  - build_enhancements dispatches kind="reflection" to a POST_APPLY_TEST tuple;
  - react_loop._format_retry_feedback renders the note FIRST (before the raw
    test_output appendix) wrapped in UNTRUSTED:reflection delimiters;
  - apply_change_and_test._finalize fires the hook only on failure, strips
    the transient _budget, and merges only the keys a callback changed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from enhancements.hooks import HookName, HookRegistry  # noqa: E402
from enhancements.build_enhancements import build_enhancements  # noqa: E402
from enhancements.reflection import make_reflection_callback  # noqa: E402
from services.budget import RunBudget  # noqa: E402


# ── fakes ─────────────────────────────────────────────────────────────────────


class _FakeMsg:
    def __init__(self, content: str):
        self.content = content
        self.usage_metadata = {"input_tokens": 11, "output_tokens": 7}


class _FakeLLM:
    def __init__(self, content="WRONG_HYPOTHESIS: x\nWHY_IT_FAILED: y\nNEXT_FOCUS: z"):
        self.content = content
        self.calls: list = []

    def invoke(self, messages):
        self.calls.append(messages)
        return _FakeMsg(self.content)


class _BoomLLM:
    def invoke(self, messages):
        raise RuntimeError("backend down")


def _failed_state(**overrides) -> dict:
    state = {
        "test_passed": False,
        "error_info": "AssertionError: expected [1], got [1, 2]",
        "suspect_file_path": "append_one.py",
        "llm_result": {
            "error_reason": "shared default list",
            "fixes": [{"original": "items=[]", "replacement": "items=None"}],
        },
        "test_output": "FAILED test_append_one.py::test_independent_calls",
    }
    state.update(overrides)
    return state


# ── callback behaviour ────────────────────────────────────────────────────────


def test_produces_structured_note_and_count():
    llm = _FakeLLM()
    reflect = make_reflection_callback(llm=llm)
    out = reflect(_failed_state())
    assert out is not None
    assert out["reflection_count"] == 1
    note = out["reflection_note"]
    assert "WRONG_HYPOTHESIS" in note and "NEXT_FOCUS" in note
    assert len(llm.calls) == 1


def test_count_increments_on_successive_reflections():
    reflect = make_reflection_callback(llm=_FakeLLM())
    out = reflect(_failed_state(reflection_count=1))
    assert out["reflection_count"] == 2


def test_apply_crash_branch_is_deterministic_no_llm():
    # apply_error set => the patch never ran. Mechanical failure, not a
    # reasoning one: deterministic note, NO LLM call, NO budget spend.
    llm = _FakeLLM()
    budget = RunBudget()
    reflect = make_reflection_callback(llm=llm)
    out = reflect(_failed_state(
        apply_error="list assignment index out of range",
        _budget=budget,
    ))
    assert out is not None
    assert out["reflection_mode"] == "apply"
    assert out["reflection_count"] == 1
    assert llm.calls == []           # no LLM
    assert budget.calls == 0         # no budget spend
    note = out["reflection_note"]
    assert "list assignment index out of range" in note   # apply_error echoed
    assert "original_line" in note and "ANCHORED ON CONTENT" in note  # contract
    assert "empty" in note.lower()                         # no-unanchored-write rule
    assert "multiple lines" in note.lower()                # insert-via-multiline rule


def test_test_failure_branch_tagged_mode_test():
    # apply_error falsy + tests failed => LLM causal post-mortem, mode "test".
    llm = _FakeLLM()
    out = make_reflection_callback(llm=llm)(_failed_state())
    assert out["reflection_mode"] == "test"
    assert len(llm.calls) == 1


def test_apply_crash_respects_cap():
    llm = _FakeLLM()
    reflect = make_reflection_callback(llm=llm, max_reflections=2)
    assert reflect(_failed_state(apply_error="boom", reflection_count=2)) is None
    assert llm.calls == []


def test_hard_cap_skips_without_llm_call():
    # The hook fires on every failure path (incl. the apply-crash path that
    # does not bump fix_retry_count), so routing does NOT bound reflection.
    # The cap must be enforced here, with no LLM call once reached.
    llm = _FakeLLM()
    reflect = make_reflection_callback(llm=llm, max_reflections=2)
    assert reflect(_failed_state(reflection_count=2)) is None
    assert reflect(_failed_state(reflection_count=5)) is None
    assert llm.calls == []
    # still acts below the cap
    assert reflect(_failed_state(reflection_count=1)) is not None
    assert len(llm.calls) == 1


def test_default_cap_is_routing_retry_limit():
    from graph.routing import MAX_FIX_RETRIES
    from enhancements.reflection import _default_cap
    assert _default_cap() == MAX_FIX_RETRIES


def test_noop_on_passing_run():
    llm = _FakeLLM()
    reflect = make_reflection_callback(llm=llm)
    assert reflect(_failed_state(test_passed=True)) is None
    assert llm.calls == []  # a green run must not pay the reflection cost


def test_budget_exhausted_skips_call():
    llm = _FakeLLM()
    reflect = make_reflection_callback(llm=llm)
    spent = RunBudget(max_calls=1)
    spent.record_call(1, 1)  # now calls(1) >= max_calls(1) → exhausted
    assert reflect(_failed_state(_budget=spent)) is None
    assert llm.calls == []


def test_budget_recorded_when_it_runs():
    reflect = make_reflection_callback(llm=_FakeLLM())
    budget = RunBudget()
    reflect(_failed_state(_budget=budget))
    assert budget.calls == 1
    assert budget.total_tokens == 18  # 11 + 7 from _FakeMsg.usage_metadata


def test_llm_failure_is_swallowed():
    reflect = make_reflection_callback(llm=_BoomLLM())
    assert reflect(_failed_state()) is None  # non-fatal: returns None, no raise


# ── factory dispatch ──────────────────────────────────────────────────────────


def test_build_enhancements_dispatches_reflection():
    tuples = build_enhancements([{"kind": "reflection"}])
    assert len(tuples) == 1
    hook_name, fn = tuples[0]
    assert hook_name == HookName.POST_APPLY_TEST
    assert callable(fn)


# ── retry-prompt rendering (react_loop) ───────────────────────────────────────


def test_retry_feedback_renders_reflection_first_and_wrapped():
    from graph.nodes.react_loop import _format_retry_feedback

    state = {
        "fix_retry_count": 1,
        "reflection_note": "## Post-mortem\nNEXT_FOCUS: use items=None sentinel",
        "llm_result": {"fixes": [{"original": "a", "replacement": "b"}]},
        "test_output": "FAILED some_test - assert x",
        "suspect_file_path": "m.py",
    }
    block = _format_retry_feedback(state)
    assert block is not None
    assert "<<<UNTRUSTED:reflection>>>" in block
    assert "NEXT_FOCUS: use items=None sentinel" in block
    # reflection must lead the raw test_output appendix
    assert block.index("UNTRUSTED:reflection") < block.index("Test output from the previous attempt")


def test_retry_feedback_absent_reflection_is_unchanged():
    from graph.nodes.react_loop import _format_retry_feedback

    state = {
        "fix_retry_count": 1,
        "llm_result": {"fixes": [{"original": "a", "replacement": "b"}]},
        "test_output": "FAILED some_test",
        "suspect_file_path": "m.py",
    }
    block = _format_retry_feedback(state)
    assert "UNTRUSTED:reflection" not in block


# ── apply node _finalize ──────────────────────────────────────────────────────


def _config(hooks: HookRegistry, budget=None) -> dict:
    return {"configurable": {"hooks": hooks, "budget": budget, "provider": object()}}


def test_finalize_noop_on_pass():
    from graph.nodes.apply_change_and_test import _finalize

    hooks = HookRegistry()
    seen = []
    hooks.register(HookName.POST_APPLY_TEST, lambda s: seen.append(1) or None)
    result = {"test_passed": True}
    assert _finalize({}, _config(hooks), result) == result
    assert seen == []  # hook not fired on a green run


def test_finalize_fires_hook_on_failure_strips_budget_merges_delta():
    from graph.nodes.apply_change_and_test import _finalize

    captured = {}

    def cb(state):
        # the transient budget must reach the callback ...
        captured["saw_budget"] = state.get("_budget")
        captured["saw_error_info"] = state.get("error_info")
        return {"reflection_note": "N", "reflection_count": 1}

    hooks = HookRegistry()
    hooks.register(HookName.POST_APPLY_TEST, cb)
    budget = RunBudget()
    state = {"error_info": "boom"}
    result = {"test_passed": False, "test_output": "fail", "fix_retry_count": 1}

    out = _finalize(state, _config(hooks, budget), result)

    assert captured["saw_budget"] is budget
    assert captured["saw_error_info"] == "boom"
    # delta merged into result ...
    assert out["reflection_note"] == "N"
    assert out["reflection_count"] == 1
    # ... original result keys preserved ...
    assert out["test_passed"] is False and out["fix_retry_count"] == 1
    # ... transient _budget never persisted into the returned state delta.
    assert "_budget" not in out


def test_finalize_noop_when_no_hook_registered():
    from graph.nodes.apply_change_and_test import _finalize

    result = {"test_passed": False, "test_output": "x"}
    assert _finalize({}, _config(HookRegistry()), result) == result
