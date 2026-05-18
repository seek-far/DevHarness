"""
inspection.acceptance — quantify the inspector against the known-defect set.

Each fixture dir under inspection/fixtures/ has an expected.json:
  - kind="buggy": must_find {defect_class, min_severity, rationale_keywords_any}
  - kind="clean": must_not_find {max_severity_allowed}  (anything above = FP)

Metrics (fixture-level):
  recall              = buggy fixtures detected / buggy fixtures
  false_positive_rate = clean fixtures flagged   / clean fixtures
  precision           = TP / (TP + clean fixtures flagged)

This makes real LLM calls (one per fixture) — it is an operator script, NOT
part of the pytest unit suite. The scoring functions are pure and unit-tested
separately with fabricated reports.

  python -m inspection.acceptance
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from inspection.base import InspectionReport
from inspection.loader import load_target

_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}
_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def score_fixture(report: InspectionReport, spec: dict) -> tuple[str, str]:
    """Return (verdict, detail). verdict ∈ {TP, FN, TN, FP, ERROR}."""
    if report.error:
        return "ERROR", report.error

    kind = spec.get("kind")
    if kind == "buggy":
        mf = spec["must_find"]
        want_rank = _RANK[mf["min_severity"]]
        kws = [k.lower() for k in mf.get("rationale_keywords_any", [])]
        for f in report.findings:
            if (f.defect_class == mf["defect_class"]
                    and _RANK.get(f.severity, 0) >= want_rank
                    and (not kws or any(k in f.rationale.lower() for k in kws))):
                return "TP", f"{f.severity}/{f.defect_class}: {f.title}"
        return "FN", f"{len(report.findings)} finding(s), none matched"

    if kind == "clean":
        ceiling = _RANK[spec["must_not_find"]["max_severity_allowed"]]
        over = [f for f in report.findings if _RANK.get(f.severity, 0) > ceiling]
        if over:
            return "FP", f"{over[0].severity}/{over[0].defect_class}: {over[0].title}"
        return "TN", "no finding above allowed severity"

    return "ERROR", f"unknown fixture kind: {kind!r}"


def evaluate(inspector, fixtures_dir: Path = _FIXTURES) -> dict:
    rows = []
    for d in sorted(p for p in fixtures_dir.iterdir() if p.is_dir()):
        ej = d / "expected.json"
        if not ej.exists():
            continue
        spec = json.loads(ej.read_text())
        report = inspector.review(load_target(d, description=d.name))
        verdict, detail = score_fixture(report, spec)
        rows.append({"fixture": d.name, "kind": spec.get("kind"),
                     "verdict": verdict, "detail": detail})

    tp = sum(r["verdict"] == "TP" for r in rows)
    fn = sum(r["verdict"] == "FN" for r in rows)
    fp = sum(r["verdict"] == "FP" for r in rows)
    tn = sum(r["verdict"] == "TN" for r in rows)
    err = sum(r["verdict"] == "ERROR" for r in rows)
    n_buggy = tp + fn
    n_clean = fp + tn
    return {
        "rows": rows,
        "recall": tp / n_buggy if n_buggy else None,
        "false_positive_rate": fp / n_clean if n_clean else None,
        "precision": tp / (tp + fp) if (tp + fp) else None,
        "counts": {"TP": tp, "FN": fn, "FP": fp, "TN": tn, "ERROR": err},
    }


def _fmt(m: dict) -> str:
    lines = [f"{'fixture':28} {'kind':6} {'verdict':8} detail"]
    for r in m["rows"]:
        lines.append(f"{r['fixture']:28} {r['kind']:6} {r['verdict']:8} "
                     f"{r['detail'][:70]}")
    c = m["counts"]
    def pct(x): return "n/a" if x is None else f"{x:.2f}"
    lines += [
        "",
        f"counts: {c}",
        f"recall={pct(m['recall'])}  "
        f"precision={pct(m['precision'])}  "
        f"false_positive_rate={pct(m['false_positive_rate'])}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from inspection.inspector import LLMInspector
    m = evaluate(LLMInspector())
    print(_fmt(m))
    return 1 if m["counts"]["ERROR"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
