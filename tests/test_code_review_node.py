"""Phase 2 SHADOW reviewer: the code_review node records findings but must
NEVER affect the fix.

Load-bearing properties:
  - disabled → pure pass-through (no inspector call, no state change);
  - enabled → records status / finding_count / would_escalate / findings,
    and ALWAYS proceeds to commit (no routing branch exists);
  - a high/high finding is recorded as would_escalate=True but the run is
    NOT altered (decoupled measurement; zero fix_rate risk).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "bf_worker"))

from inspection.base import Finding, InspectionReport  # noqa: E402
from graph.nodes.code_review import CodeReviewConfig, code_review  # noqa: E402
from graph.routing import (  # noqa: E402
    route_after_apply_and_test,
    route_after_code_review,
)


class _Provider:
    def fetch_file(self, path, ref="main"):
        return "def f():\n    return 1\n"


class _Inspector:
    def __init__(self, report):
        self._r = report
        self.calls = 0

    def review(self, target):
        self.calls += 1
        return self._r


def _report(*findings, error=None):
    return InspectionReport("1", "t", "fake", findings=list(findings), error=error)


def _high_high():
    return Finding("high", "blind-index-or-unchecked-write", "f.py", 1,
                   "bad", "silent corruption", "fix it", "high")


def _cfg(code_review_cfg, provider=None, budget=None):
    return {"configurable": {
        "code_review": code_review_cfg,
        "provider": provider or _Provider(),
        "budget": budget,
    }}


_STATE = {"bug_id": "B1", "suspect_file_path": "f.py",
          "llm_result": {"fixes": [{"file_path": "f.py", "line_number": 1,
                                    "original_line": "x", "new_line": "y"}]}}


# ── disabled = pure pass-through (baseline byte-identical) ─────────────────────


def test_disabled_is_passthrough_no_inspector_call():
    insp = _Inspector(_report(_high_high()))
    out = code_review(_STATE, _cfg(CodeReviewConfig(enabled=False, inspector=insp)))
    assert out == {}
    assert insp.calls == 0


def test_no_code_review_key_is_passthrough():
    assert code_review(_STATE, {"configurable": {"provider": _Provider()}}) == {}


# ── shadow mode (DEFAULT): records, never acts ────────────────────────────────


def test_default_mode_is_shadow():
    assert CodeReviewConfig(enabled=True).mode == "shadow"


def test_shadow_high_high_recorded_as_would_escalate_only():
    insp = _Inspector(_report(_high_high()))
    out = code_review(_STATE, _cfg(CodeReviewConfig(enabled=True, inspector=insp)))
    assert out["code_review_status"] == "would_escalate"
    assert out["code_review_would_escalate"] is True
    assert out["code_review_finding_count"] == 1
    assert out["code_review_findings"][0]["severity"] == "high"
    # shadow never feeds back: note stays None, no routing key.
    assert out["code_review_note"] is None
    assert insp.calls == 1


def test_shadow_non_high_high_is_advisory():
    f = Finding("high", "other", "f.py", 1, "t", "r", "s", "low")  # high sev, low conf
    out = code_review(_STATE, _cfg(CodeReviewConfig(enabled=True,
                                                    inspector=_Inspector(_report(f)))))
    assert out["code_review_status"] == "advisory"
    assert out["code_review_would_escalate"] is False


def test_clean_when_no_findings():
    out = code_review(_STATE, _cfg(CodeReviewConfig(enabled=True,
                                                    inspector=_Inspector(_report()))))
    assert out["code_review_status"] == "clean"
    assert out["code_review_would_escalate"] is False


# ── acting mode (EXPLICIT): records AND feeds back, bounded ───────────────────


def test_acting_high_high_escalates_with_note():
    insp = _Inspector(_report(_high_high()))
    out = code_review(_STATE, _cfg(
        CodeReviewConfig(enabled=True, mode="acting", max_rounds=1, inspector=insp)))
    assert out["code_review_status"] == "escalated"
    assert out["code_review_rounds"] == 1
    assert out["code_review_would_escalate"] is True
    assert "code review" in out["code_review_note"].lower()


def test_acting_round_cap_exhausted_ships():
    insp = _Inspector(_report(_high_high()))
    st = dict(_STATE, code_review_rounds=1)
    out = code_review(st, _cfg(
        CodeReviewConfig(enabled=True, mode="acting", max_rounds=1, inspector=insp)))
    assert out["code_review_status"] == "rounds_exhausted"
    assert out.get("code_review_note") is None      # does not block shipping


def test_acting_without_high_high_is_advisory_not_escalated():
    f = Finding("high", "other", "f.py", 1, "t", "r", "s", "low")
    out = code_review(_STATE, _cfg(
        CodeReviewConfig(enabled=True, mode="acting", inspector=_Inspector(_report(f)))))
    assert out["code_review_status"] == "advisory"
    assert out["code_review_note"] is None


def test_inspector_failure_is_swallowed():
    class _Boom:
        def review(self, t): raise RuntimeError("down")
    out = code_review(_STATE, _cfg(CodeReviewConfig(enabled=True, inspector=_Boom())))
    assert out["code_review_status"] == "error"


def test_budget_exhausted_skips():
    from services.budget import RunBudget
    spent = RunBudget(max_calls=1)
    spent.record_call(1, 1)
    insp = _Inspector(_report(_high_high()))
    out = code_review(_STATE, _cfg(CodeReviewConfig(enabled=True, inspector=insp),
                                   budget=spent))
    assert out["code_review_status"] == "skipped_budget"
    assert insp.calls == 0


def test_no_files_skips():
    out = code_review({"bug_id": "B"}, _cfg(CodeReviewConfig(enabled=True,
                                                             inspector=_Inspector(_report()))))
    assert out["code_review_status"] == "skipped_no_files"


# ── routing ───────────────────────────────────────────────────────────────────


def test_route_after_apply_test_passed_goes_to_code_review():
    assert route_after_apply_and_test({"test_passed": True}) == "code_review"


def test_route_after_apply_failure_paths_unchanged():
    assert route_after_apply_and_test({"test_passed": False,
                                       "fix_retry_count": 0}) == "react_loop"
    assert route_after_apply_and_test({"test_passed": False,
                                       "fix_retry_count": 9}) == "handle_failure"


def test_route_after_code_review_only_escalated_branches():
    # Only acting mode ever sets "escalated"; everything else (incl. shadow's
    # "would_escalate") proceeds to commit — the reviewer never blocks.
    assert route_after_code_review({"code_review_status": "escalated"}) == "react_loop"
    for s in ("would_escalate", "clean", "advisory", "rounds_exhausted",
              "error", "skipped_budget", None):
        assert route_after_code_review({"code_review_status": s}) == "commit_change"


# ── retry prompt: shadow note absent → not rendered; acting note → rendered ────


def test_retry_feedback_renders_code_review_note_when_set():
    from graph.nodes.react_loop import _format_retry_feedback
    base = {"fix_retry_count": 1,
            "llm_result": {"fixes": [{"line_number": 1, "original_line": "a",
                                      "new_line": "b"}]},
            "test_output": "x"}
    # shadow: note is None → nothing rendered
    assert "UNTRUSTED:code_review" not in _format_retry_feedback(
        dict(base, code_review_note=None))
    # acting: note set → rendered, wrapped untrusted
    block = _format_retry_feedback(dict(base, code_review_note="## review flagged Y"))
    assert "<<<UNTRUSTED:code_review>>>" in block
    assert "review flagged Y" in block


# ── agent kwarg normalization + graph builds ──────────────────────────────────


def test_build_code_review_config_normalization():
    from agents.langgraph_agent import _build_code_review_config
    assert _build_code_review_config(None).enabled is False
    assert _build_code_review_config(False).enabled is False
    assert _build_code_review_config(True).enabled is True
    assert _build_code_review_config(True).mode == "shadow"   # default
    c = _build_code_review_config({"enabled": True, "mode": "acting",
                                   "max_rounds": 3})
    assert c.enabled is True and c.mode == "acting" and c.max_rounds == 3
    cc = CodeReviewConfig(enabled=True, mode="acting")
    assert _build_code_review_config(cc) is cc


def test_graph_compiles_with_code_review_node():
    from graph.builder import build_graph
    assert build_graph() is not None
