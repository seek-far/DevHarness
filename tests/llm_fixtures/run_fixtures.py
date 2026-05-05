"""Real-LLM driver for the parse/fetch fallback fixtures.

Runs each fixture under tests/llm_fixtures/<name>/ through standalone mode,
reads the most recent journal entry written for the bug_id, and verifies the
fallback flags + outcome match meta.json. Prints a per-fixture report.

Usage:
    source .venv-linux/bin/activate && uv run python tests/llm_fixtures/run_fixtures.py [<name> ...]

If fixture names are passed, only those are run; otherwise all four run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = Path(__file__).resolve().parent
JOURNAL_DIR = REPO_ROOT / "evaluation" / "journal"


def _latest_journal_for_bug(bug_id: str):
    """Find the most recent journal subdir whose record has matching bug_id.

    Journal entries are named <ts>_<bug_id>_<agent>[_<model_slug>].
    """
    if not JOURNAL_DIR.exists():
        return None
    candidates = sorted(
        [d for d in JOURNAL_DIR.iterdir() if d.is_dir() and f"_{bug_id}_" in d.name],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    record_path = candidates[0] / "record.json"
    if not record_path.exists():
        return None
    return json.loads(record_path.read_text(encoding="utf-8"))


def _run_one(fixture_dir: Path) -> dict:
    name = fixture_dir.name
    meta = json.loads((fixture_dir / "meta.json").read_text(encoding="utf-8"))

    # Use a unique bug_id per run so the journal entry can't be confused with
    # an earlier one. Append the current PID + a counter.
    bug_id = f"BUG-LLM-{name}-{os.getpid()}"

    # Copy source/ into a clean temp dir so the LLM operates on an isolated
    # working copy (LocalNoGitProvider already does this internally, but we
    # also keep --output-dir separate).
    tmp_root = Path(tempfile.mkdtemp(prefix=f"llmfix_{name}_"))
    work_source = tmp_root / "source"
    shutil.copytree(fixture_dir / "source", work_source)
    out_dir = tmp_root / "out"
    out_dir.mkdir()

    cmd = [
        "uv", "run", "python", "-m", "bf_worker.standalone",
        "--source-dir", str(work_source),
        "--trace-file", str(fixture_dir / "trace.txt"),
        "--bug-id", bug_id,
        "--no-git",
        "--output-dir", str(out_dir),
    ]
    print(f"\n[{name}] running: {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO_ROOT))
    print(f"[{name}] standalone exit={proc.returncode}")
    # Last 30 lines of stderr is usually enough to see what happened.
    tail = "\n".join(proc.stderr.splitlines()[-30:])
    print(f"[{name}] stderr tail:\n{tail}")

    # Read the journal entry.
    record = _latest_journal_for_bug(bug_id)
    if record is None:
        return {
            "name": name,
            "ok": False,
            "reason": f"no journal entry found for bug_id={bug_id}",
            "stderr_tail": tail,
        }

    # Verify outcome + flags.
    failures = []
    if record.get("outcome") != meta["expected_outcome"]:
        failures.append(
            f"outcome={record.get('outcome')!r} (expected {meta['expected_outcome']!r})"
        )
    for k, expected in meta["expected_flags"].items():
        if k == "suspect_file_path_contains":
            actual = record.get("suspect_file_path") or ""
            if expected not in actual:
                failures.append(
                    f"suspect_file_path={actual!r} does not contain {expected!r}"
                )
        elif k == "suspect_file_path":
            if record.get("suspect_file_path") != expected:
                failures.append(
                    f"suspect_file_path={record.get('suspect_file_path')!r} "
                    f"(expected {expected!r})"
                )
        else:
            actual = record.get(k)
            if actual != expected:
                failures.append(f"{k}={actual!r} (expected {expected!r})")

    return {
        "name": name,
        "ok": not failures,
        "outcome": record.get("outcome"),
        "iterations": record.get("iterations"),
        "react_step_count": record.get("react_step_count"),
        "parse_trace_fallback": record.get("parse_trace_fallback"),
        "source_fetch_failed": record.get("source_fetch_failed"),
        "suspect_file_path": record.get("suspect_file_path"),
        "test_passed": record.get("test_passed"),
        "elapsed_s": record.get("elapsed_s"),
        "failures": failures,
        "tmp_dir": str(tmp_root),
    }


def main(argv: list[str]) -> int:
    selected = set(argv[1:]) if len(argv) > 1 else None
    fixture_dirs = sorted(
        d for d in FIXTURES_DIR.iterdir()
        if d.is_dir() and (d / "meta.json").exists()
    )
    if selected:
        fixture_dirs = [d for d in fixture_dirs if d.name in selected]

    results = []
    for d in fixture_dirs:
        results.append(_run_one(d))

    print("\n" + "=" * 78)
    print("Fixture results")
    print("=" * 78)
    for r in results:
        status = "PASS" if r["ok"] else "FAIL"
        print(f"  [{status}] {r['name']}")
        for k in (
            "outcome", "iterations", "react_step_count",
            "parse_trace_fallback", "source_fetch_failed",
            "suspect_file_path", "test_passed", "elapsed_s",
        ):
            if k in r:
                print(f"      {k}: {r[k]!r}")
        if r.get("failures"):
            for f in r["failures"]:
                print(f"      ⚠ {f}")
        if not r["ok"] and "reason" in r:
            print(f"      reason: {r['reason']}")

    n_pass = sum(1 for r in results if r["ok"])
    print(f"\n{n_pass}/{len(results)} fixtures passed")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
