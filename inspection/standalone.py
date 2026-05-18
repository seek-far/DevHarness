"""
standalone.py — run the code inspection agent against a path.

  python -m inspection.standalone --path bf_worker/services/apply_patch.py
  python -m inspection.standalone --path bf_worker/ --json report.json
  python -m inspection.standalone --path X --fail-on high   # CI gate

Independent of the bug-fix pipeline. Exit code is non-zero when a finding at
or above --fail-on severity is reported (default: never fail the process).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from inspection.base import SEVERITIES
from inspection.inspector import LLMInspector
from inspection.loader import load_target

_SEV_RANK = {s: i for i, s in enumerate(reversed(SEVERITIES))}  # high=3 .. info=0


def _format(report) -> str:
    if report.error:
        return f"[inspection error] {report.error}"
    if not report.findings:
        return f"No findings. {report.summary}".strip()
    lines = [f"Summary: {report.summary}", ""]
    for i, f in enumerate(report.findings, 1):
        loc = f"{f.file}:{f.line}" if f.line else f.file
        lines += [
            f"{i}. [{f.severity}/{f.defect_class}] {f.title}  ({loc})",
            f"   why: {f.rationale}",
            f"   fix: {f.suggestion}",
        ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="inspection.standalone")
    ap.add_argument("--path", required=True, help="file or directory to inspect")
    ap.add_argument("--description", default="", help="optional target description")
    ap.add_argument("--json", default="", help="write the JSON report to this file")
    ap.add_argument("--fail-on", choices=SEVERITIES, default=None,
                    help="exit non-zero if a finding at/above this severity exists")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    target = load_target(args.path, description=args.description)
    report = LLMInspector().review(target)

    print(_format(report))
    if args.json:
        Path(args.json).write_text(report.to_json(), encoding="utf-8")
        print(f"\n[json report written: {args.json}]")

    if report.error:
        return 2
    if args.fail_on:
        threshold = _SEV_RANK[args.fail_on]
        if any(_SEV_RANK.get(f.severity, 0) >= threshold for f in report.findings):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
