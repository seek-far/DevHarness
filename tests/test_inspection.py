"""Unit tests for the standalone code inspection agent (Phase 1).

No network: the LLM is injected as a fake (same pattern as the reflection
tests). Includes the canonical acceptance check — given the pre-A
`apply_change_infos` ground-truth fixture, the inspector must surface a
high-severity blind-index-write finding (validated against expected.json).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from inspection.base import (  # noqa: E402
    CodeFile,
    CodeTarget,
    Finding,
    InspectionReport,
)
from inspection.acceptance import evaluate, score_fixture  # noqa: E402
from inspection.inspector import LLMInspector, _build_user_prompt, _parse  # noqa: E402
from inspection.loader import load_target  # noqa: E402


def _report(*findings, error=None):
    return InspectionReport("1", "t", "m", findings=list(findings), error=error)

_FIXTURE = _ROOT / "inspection" / "fixtures" / "blind_apply_patch"


class _FakeLLM:
    def __init__(self, content: str):
        self.content = content
        self.calls: list = []

    def invoke(self, messages):
        self.calls.append(messages)

        class _M:
            pass
        m = _M()
        m.content = self.content
        return m


_BLIND_WRITE_JSON = json.dumps({
    "summary": "blind index write in apply_change_infos",
    "findings": [{
        "severity": "high",
        "defect_class": "blind-index-or-unchecked-write",
        "file": "apply_patch.py",
        "line": 13,
        "title": "src_lines[line_number-1] assigned without checking original_line",
        "rationale": "A stale line_number silently corrupts an already-correct "
                     "file; no test catches it.",
        "suggestion": "Anchor on original_line; reject/relocate on mismatch.",
    }],
})


# ── loader ────────────────────────────────────────────────────────────────────


def test_loader_single_file():
    t = load_target(_FIXTURE / "apply_patch.py")
    assert len(t.files) == 1
    assert t.files[0].path == "apply_patch.py"
    assert "apply_change_infos" in t.files[0].content


def test_loader_directory_skips_junk(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "b.py").write_text("junk\n")
    t = load_target(tmp_path)
    paths = {f.path for f in t.files}
    assert "a.py" in paths
    assert not any("__pycache__" in p for p in paths)


def test_loader_missing_path_raises():
    with pytest.raises(FileNotFoundError):
        load_target("/no/such/path/xyz")


# ── schema ────────────────────────────────────────────────────────────────────


def test_finding_normalized_clamps_bad_values():
    f = Finding("BOGUS", "not-a-class", "f.py", 1, "t", "r", "s").normalized()
    assert f.severity == "info"
    assert f.defect_class == "other"


def test_report_json_roundtrips():
    r = InspectionReport("1", "t", "m",
                          findings=[Finding("high", "other", "f", 2, "x", "y", "z")])
    d = json.loads(r.to_json())
    assert d["findings"][0]["severity"] == "high"
    assert d["schema_version"] == "1"


# ── prompt + parser ───────────────────────────────────────────────────────────


def test_prompt_includes_code_and_niche():
    t = CodeTarget(files=[CodeFile("m.py", "def f():\n    return 1")],
                   description="demo")
    p = _build_user_prompt(t)
    assert "FILE: m.py" in p
    assert "1| def f():" in p          # numbered
    assert "demo" in p


def test_parse_tolerates_fenced_json():
    summary, findings = _parse("```json\n" + _BLIND_WRITE_JSON + "\n```")
    assert "blind index" in summary
    assert findings[0].defect_class == "blind-index-or-unchecked-write"


def test_parse_empty_findings_ok():
    s, f = _parse('{"summary": "clean", "findings": []}')
    assert f == [] and s == "clean"


def test_parse_junk_raises():
    with pytest.raises(ValueError):
        _parse("the model rambled with no json")


# ── inspector end-to-end (fake LLM) ───────────────────────────────────────────


def test_review_surfaces_finding():
    insp = LLMInspector(llm=_FakeLLM(_BLIND_WRITE_JSON), model="fake")
    rep = insp.review(load_target(_FIXTURE / "apply_patch.py"))
    assert rep.error is None
    assert rep.has_findings
    assert rep.findings[0].severity == "high"


def test_review_empty_target_errors():
    rep = LLMInspector(llm=_FakeLLM("{}")).review(CodeTarget())
    assert rep.error and "empty target" in rep.error


def test_review_llm_failure_is_captured_not_raised():
    class _Boom:
        def invoke(self, m): raise RuntimeError("backend down")
    rep = LLMInspector(llm=_Boom()).review(
        CodeTarget(files=[CodeFile("a.py", "x=1")]))
    assert rep.error and "llm call failed" in rep.error
    assert rep.findings == []


def test_review_unparseable_response_captured():
    rep = LLMInspector(llm=_FakeLLM("no json here")).review(
        CodeTarget(files=[CodeFile("a.py", "x=1")]))
    assert rep.error and "unparseable" in rep.error


# ── canonical acceptance: ground-truth fixture vs expected.json ───────────────


# ── acceptance scorer (pure, no network) ──────────────────────────────────────


_BUGGY_SPEC = {"kind": "buggy", "must_find": {
    "defect_class": "blind-index-or-unchecked-write", "min_severity": "high",
    "rationale_keywords_any": ["original_line", "silent"]}}
_CLEAN_SPEC = {"kind": "clean", "must_not_find": {"max_severity_allowed": "low"}}


def test_score_buggy_tp():
    r = _report(Finding("high", "blind-index-or-unchecked-write", "a.py", 1,
                         "t", "ignores original_line; silent corruption", "s"))
    assert score_fixture(r, _BUGGY_SPEC)[0] == "TP"


def test_score_buggy_fn_wrong_class_or_severity_or_kw():
    assert score_fixture(_report(Finding("high", "other", "a", 1, "t", "silent original_line", "s")), _BUGGY_SPEC)[0] == "FN"
    assert score_fixture(_report(Finding("low", "blind-index-or-unchecked-write", "a", 1, "t", "silent original_line", "s")), _BUGGY_SPEC)[0] == "FN"
    assert score_fixture(_report(Finding("high", "blind-index-or-unchecked-write", "a", 1, "t", "unrelated reason", "s")), _BUGGY_SPEC)[0] == "FN"


def test_score_buggy_defect_class_list_accepts_any():
    spec = {"kind": "buggy", "must_find": {
        "defect_class": ["silent-empty-or-wrong-key", "contract-mismatch"],
        "min_severity": "medium", "rationale_keywords_any": ["original_line"]}}
    r = _report(Finding("high", "contract-mismatch", "a.py", 8, "t",
                         "wrong key, original_line missing", "s"))
    assert score_fixture(r, spec)[0] == "TP"
    r2 = _report(Finding("high", "other", "a.py", 8, "t",
                          "original_line", "s"))
    assert score_fixture(r2, spec)[0] == "FN"


def test_score_clean_tn_and_fp():
    assert score_fixture(_report(), _CLEAN_SPEC)[0] == "TN"
    assert score_fixture(_report(Finding("low", "other", "a", 1, "t", "r", "s")), _CLEAN_SPEC)[0] == "TN"
    assert score_fixture(_report(Finding("medium", "other", "a", 1, "t", "r", "s")), _CLEAN_SPEC)[0] == "FP"


def test_score_error_report():
    assert score_fixture(_report(error="llm down"), _BUGGY_SPEC)[0] == "ERROR"


def test_evaluate_metrics_math(tmp_path):
    # one buggy + one clean fixture; stub inspector returns canned reports.
    (tmp_path / "bug").mkdir()
    (tmp_path / "bug" / "m.py").write_text("x=1\n")
    (tmp_path / "bug" / "expected.json").write_text(json.dumps(_BUGGY_SPEC))
    (tmp_path / "ok").mkdir()
    (tmp_path / "ok" / "m.py").write_text("y=2\n")
    (tmp_path / "ok" / "expected.json").write_text(json.dumps(_CLEAN_SPEC))

    class _Stub:
        def review(self, target):
            if target.description == "bug":
                return _report(Finding("high", "blind-index-or-unchecked-write",
                                       "m.py", 1, "t", "silent original_line", "s"))
            return _report()  # clean → no findings

    m = evaluate(_Stub(), fixtures_dir=tmp_path)
    assert m["counts"] == {"TP": 1, "FN": 0, "FP": 0, "TN": 1, "ERROR": 0}
    assert m["recall"] == 1.0 and m["precision"] == 1.0
    assert m["false_positive_rate"] == 0.0


def test_blind_apply_patch_acceptance():
    """The Phase-1 contract: the inspector must catch the real pre-A defect.

    Driven by a fake LLM here (unit suite must not hit the network); the
    assertion enforces the expected.json acceptance spec so a real run that
    produces an equivalent finding passes the same gate.
    """
    spec = json.loads((_FIXTURE / "expected.json").read_text())["must_find"]
    rep = LLMInspector(llm=_FakeLLM(_BLIND_WRITE_JSON)).review(
        load_target(_FIXTURE / "apply_patch.py"))

    hit = [f for f in rep.findings if f.defect_class == spec["defect_class"]]
    assert hit, f"no finding of class {spec['defect_class']}"
    rank = {"info": 0, "low": 1, "medium": 2, "high": 3}
    assert max(rank[f.severity] for f in hit) >= rank[spec["min_severity"]]
    blob = " ".join(f.rationale.lower() for f in hit)
    assert any(k.lower() in blob for k in spec["rationale_keywords_any"])
