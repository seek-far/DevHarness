#!/usr/bin/env python3
"""Resolved verdict for a ver99 CI run — via SWE-bench's OFFICIAL parser + grading.

Committed onto each instance branch as `.swebench/grade.py`. The CI job runs the
authoritative whole-file/module test command (wrapped in SWE-bench's Start/End
markers), captures the output to a log, then invokes this to decide RESOLVED.

Why not gate on the test command's exit code: SWE-bench grades by PARSING the log
for the specific FAIL_TO_PASS / PASS_TO_PASS test names — not the exit code. A
whole-module run can contain unrelated failing/erroring tests (verified on
django__django-15098: gold patch → whole module exits 1 due to an unrelated
error, yet the instance IS resolved). Exit-code gating would mark that a false
negative. This uses the same parser SWE-bench's harness uses, so the CI verdict
matches the official one.

Usage:  grade.py <test_output_log>   → exit 0 iff RESOLVED (all FAIL_TO_PASS and
PASS_TO_PASS pass). Reads `full_instance.json` (the full SWE-bench row) beside it.

Requires `swebench` importable in the job (the CI `pip install swebench`s it; the
eval image does not ship it).
"""
import json
import sys
from pathlib import Path

from swebench.harness.grading import (
    get_logs_eval, get_eval_tests_report, get_resolution_status,
    FAIL_TO_PASS, PASS_TO_PASS,
)
from swebench.harness.constants import ResolvedStatus


def _ld(v):
    return json.loads(v) if isinstance(v, str) else v


class _SpecShim:
    """The two attributes `get_logs_eval` actually reads off a TestSpec (it uses
    them to look up the per-repo log parser + test_cmd).

    Building a real TestSpec via `make_test_spec()` DOWNLOADS the repo's
    requirements file from raw.githubusercontent.com (django et al.) — a network
    dependency inside the CI hot path. On a host that can't reach GitHub that is
    a 4-5 min stall per job or an outright ConnectionError; even where it works
    it buys grading nothing. Grading is pure log parsing, so keep it offline.
    """

    def __init__(self, inst: dict):
        self.repo = inst["repo"]
        self.version = inst["version"]
        self.instance_id = inst["instance_id"]


def main() -> None:
    inst = json.loads((Path(__file__).parent / "full_instance.json").read_text())
    ts = _SpecShim(inst)
    status_map, _ok = get_logs_eval(ts, sys.argv[1])
    gold = {FAIL_TO_PASS: _ld(inst["FAIL_TO_PASS"]), PASS_TO_PASS: _ld(inst["PASS_TO_PASS"])}
    report = get_eval_tests_report(status_map, gold)
    resolved = get_resolution_status(report) == ResolvedStatus.FULL.value
    f2p, p2p = report[FAIL_TO_PASS], report[PASS_TO_PASS]
    print(f"[grade] resolved={resolved} "
          f"FAIL_TO_PASS {len(f2p['success'])}/{len(f2p['success']) + len(f2p['failure'])} pass; "
          f"PASS_TO_PASS {len(p2p['success'])}/{len(p2p['success']) + len(p2p['failure'])} pass")
    sys.exit(0 if resolved else 1)


if __name__ == "__main__":
    main()
