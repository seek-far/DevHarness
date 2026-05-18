"""
Node: code_review  (Phase 2 — the standalone inspection agent, wired into
the bug-fix graph as a reviewer of the test-passing patch).

Fires only on the test-PASSED branch, before commit_change. The patch is
green, so this is where "green but wrong" defects (the ones the test oracle
misses) would surface.

Two MODES (config-selected; default shadow):

  shadow  — records findings + whether it *would* have escalated, but NEVER
            affects the fix (no react_loop feedback, no routing branch;
            always proceeds to commit). Decouples measurement from
            intervention: quantify the reviewer's would-be value at ZERO
            fix_rate risk before trusting it to act.

  acting  — explicit opt-in. Same recording, PLUS: a would-escalate finding
            (severity==high AND confidence==high) feeds an advisory note
            back into react_loop for a bounded extra fixer round
            (code_review_rounds, independent of fix_retry_count). Never
            blocks shipping a green patch (rounds exhausted / clean /
            advisory / skipped / error → commit).

Disabled by default → pure pass-through (no inspector call, telemetry stays
None, baseline byte-identical). Any inspector failure is swallowed (status
"error"); it can never break the core run.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from langchain_core.runnables import RunnableConfig

from graph.state import BugFixState
from services.runtime_context import get_budget, get_code_review, get_provider

# inspection/ is a top-level package (sibling of bf_worker/); add repo root.
sys.path.append(str(Path(__file__).resolve().parents[3]))
from inspection.base import CodeFile, CodeTarget  # noqa: E402

logger = logging.getLogger(__name__)


@dataclass
class CodeReviewConfig:
    """Run-scoped code-review settings (lives in config['configurable'])."""

    enabled: bool = False
    mode: str = "shadow"   # "shadow" (default, never acts) | "acting"
    max_rounds: int = 1    # acting mode only: bounded code_review→react_loop loop
    inspector: Any = None  # defaults to a lazily-built LLMInspector

    def get_inspector(self):
        if self.inspector is None:
            from inspection.inspector import LLMInspector
            self.inspector = LLMInspector()
        return self.inspector


def _compact(findings) -> list[dict]:
    return [
        {
            "severity": f.severity,
            "confidence": f.confidence,
            "defect_class": f.defect_class,
            "file": f.file,
            "line": f.line,
            "title": f.title,
        }
        for f in findings
    ]


def _format_note(findings) -> str:
    lines = [
        "## Code review of your test-passing patch flagged HIGH-confidence, "
        "HIGH-severity defect(s) the tests do not catch — revise to address "
        "them (the tests still pass; this is about correctness the tests "
        "miss):"
    ]
    for i, f in enumerate(findings, 1):
        loc = f"{f.file}:{f.line}" if f.line else f.file
        lines += [
            f"{i}. [{f.defect_class}] {f.title}  ({loc})",
            f"   why: {f.rationale}",
            f"   fix: {f.suggestion}",
        ]
    return "\n".join(lines)


def code_review(state: BugFixState, config: Optional[RunnableConfig] = None) -> dict:
    cr = get_code_review(config)
    if not cr or not getattr(cr, "enabled", False):
        return {}  # disabled → pass-through; baseline byte-identical

    budget = get_budget(config)
    if budget is not None and budget.check() is not None:
        logger.info("code_review: budget exhausted, skipping")
        return {"code_review_status": "skipped_budget"}

    provider = get_provider(config)
    paths: list[str] = []
    sp = state.get("suspect_file_path")
    if sp:
        paths.append(sp)
    for f in (state.get("llm_result") or {}).get("fixes") or []:
        fp = f.get("file_path")
        if fp and fp not in paths:
            paths.append(fp)

    files: list[CodeFile] = []
    for p in paths:
        try:
            files.append(CodeFile(path=p, content=provider.fetch_file(p)))
        except Exception as exc:
            logger.warning("code_review: could not read %s: %s", p, exc)
    if not files:
        return {"code_review_status": "skipped_no_files"}

    target = CodeTarget(
        files=files,
        description=f"post-fix review for {state.get('bug_id', '')}",
    )
    try:
        report = cr.get_inspector().review(target)
    except Exception as exc:
        # Never break the core run — same contract as the enhancements.
        logger.warning("code_review: inspector failed (non-fatal): %s", exc)
        return {"code_review_status": "error"}

    if budget is not None:
        # Count the call so max_calls still bounds it. Inspector token usage
        # is not surfaced by its contract — a documented limitation.
        budget.record_call(0, 0)

    fc = len(report.findings)
    out: dict[str, Any] = {
        "code_review_finding_count": fc,
        "code_review_findings": _compact(report.findings),
    }
    if report.error:
        out["code_review_status"] = "error"
        return out

    escalating = [
        f for f in report.findings
        if f.severity == "high" and f.confidence == "high"
    ]
    would_escalate = bool(escalating)
    out["code_review_would_escalate"] = would_escalate
    mode = getattr(cr, "mode", "shadow")

    if mode == "acting" and would_escalate:
        rounds = int(state.get("code_review_rounds") or 0)
        if rounds < cr.max_rounds:
            logger.info(
                "code_review[acting]: escalating %d high/high finding(s) — "
                "round %d/%d", len(escalating), rounds + 1, cr.max_rounds,
            )
            out.update(
                code_review_status="escalated",
                code_review_rounds=rounds + 1,
                code_review_note=_format_note(escalating),
            )
            return out
        out["code_review_status"] = "rounds_exhausted"
        logger.info("code_review[acting]: round cap reached — shipping")
        return out

    # shadow (any), or acting without a would-escalate finding: record only.
    out["code_review_status"] = (
        "would_escalate" if would_escalate
        else ("advisory" if fc else "clean")
    )
    out["code_review_note"] = None
    logger.info(
        "code_review[%s]: %s — %d finding(s), would_escalate=%s (recorded%s)",
        mode, out["code_review_status"], fc, would_escalate,
        "" if mode == "acting" else "; shadow never acts",
    )
    return out
