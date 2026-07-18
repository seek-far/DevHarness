"""
swebench_batch.py — run the mini-swe-agent substrate over MANY SWE-bench instances.

Batch sibling of bf_worker/swebench_single.py. Selection is the full subset by
default, or a specified set via --instances / --instances-file / --filter /
--slice / --shuffle. Instances run in parallel (ThreadPool); the whole batch is
then graded with the official SWE-bench harness and a resolved-rate is reported.

Like evaluation/runner.py this is EVALUATION mode — it writes to
evaluation/runs/<run_id>/ (preds.json + records/<id>.json + summary.json), NOT
the running-mode journal. `bench report <run_id>` reads summary.json (metrics
surfaces resolved-rate additively for SWE-bench runs).

Fully resumable: by default skips instances already in the run's preds.json, so
re-running the same --run-id continues an interrupted batch. --redo-existing
forces a full re-run.

    python -m bf_worker.swebench_batch --subset verified --split test --slice 0:10
    python -m bf_worker.swebench_batch --instances sympy__sympy-22914,sympy__sympy-23950
    python -m bf_worker.swebench_batch --instances-file ids.txt --workers 8

Requirements: docker, `datasets`, `swebench`, mini's runtime deps (litellm, …) —
see evaluation/requirements-swebench.txt. Heavy; NOT in the service images.
Full Verified is ~500 instances × multi-GB images — run a subset first.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")
# A batch over the same model must not abort on the first unpriced LLM call:
# with ignore_errors, litellm still computes cost for priced models and simply
# reports 0 (→ our adapter records None) for models it can't price, instead of
# raising and killing every instance. See docs/swebench.md.
os.environ.setdefault("MSWEA_COST_TRACKING", "ignore_errors")

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

from agents.base import BugInput, FixOutput  # noqa: E402
from agents.mini_swe_agent import MiniSweAgent  # noqa: E402
from agents.run_record import RunRecord  # noqa: E402
from swebench_single import DATASET_MAPPING, build_mini_config, grade_with_harness  # noqa: E402

logger = logging.getLogger(__name__)

_RUNS_ROOT = _HERE.parent / "evaluation" / "runs"
_PREDS_LOCK = threading.Lock()


# ── instance selection ───────────────────────────────────────────────────────

def filter_instances(
    instances: list[dict],
    *,
    ids: list[str] | None = None,
    filter_re: str = "",
    slice_spec: str = "",
    shuffle: bool = False,
) -> list[dict]:
    """Pure selection over a list of instance dicts (no dataset I/O — unit-tested).

    Order of operations: explicit ids (preserves given order) → shuffle → regex
    filter → slice. `ids` short-circuits the other selectors' defaulting: when
    given, only those instances (in that order) are the candidate set.
    """
    by_id = {i["instance_id"]: i for i in instances}
    if ids:
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise SystemExit(f"unknown instance id(s): {', '.join(missing)}")
        selected = [by_id[i] for i in ids]
    else:
        selected = [by_id[k] for k in sorted(by_id)]

    if shuffle:
        selected = list(selected)
        random.seed(42)
        random.shuffle(selected)
    if filter_re:
        rx = re.compile(filter_re)
        selected = [i for i in selected if rx.search(i["instance_id"])]
    if slice_spec:
        parts = [int(x) if x else None for x in slice_spec.split(":")]
        selected = selected[slice(*parts)]
    return selected


def select_instances(subset, split, *, ids, ids_file, filter_re, slice_spec, shuffle) -> list[dict]:
    from datasets import load_dataset

    dataset_path = DATASET_MAPPING.get(subset, subset)
    logger.info("loading dataset %s split %s ...", dataset_path, split)
    instances = [dict(r) for r in load_dataset(dataset_path, split=split)]

    wanted: list[str] = list(ids or [])
    if ids_file:
        for line in Path(ids_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                wanted.append(line)
    return filter_instances(
        instances, ids=wanted or None, filter_re=filter_re, slice_spec=slice_spec, shuffle=shuffle
    )


# ── preds.json (thread-safe) ─────────────────────────────────────────────────

def update_pred(preds_path: Path, instance_id: str, model_name: str, patch: str) -> None:
    with _PREDS_LOCK:
        data = json.loads(preds_path.read_text()) if preds_path.exists() else {}
        data[instance_id] = {
            "model_name_or_path": model_name or "mini_swe_agent",
            "instance_id": instance_id,
            "model_patch": patch,
        }
        preds_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ── per-instance work ────────────────────────────────────────────────────────

def run_one(
    instance: dict, mini_config: dict, model_name: str, records_dir: Path, preds_path: Path
) -> dict:
    """Fix one instance and persist its record.json + preds entry (resolved is
    backfilled after batch grading). Returns the record dict."""
    iid = instance["instance_id"]
    bug_input = BugInput(bug_id=iid, provider=None, metadata={"swebench_instance": instance})
    agent = MiniSweAgent(mini_config=mini_config, agent_config={"llm_model": model_name})

    t0 = time.monotonic()
    try:
        fix = agent.fix(bug_input)
    except Exception as exc:  # defensive — MiniSweAgent.fix already traps mini errors
        logger.exception("instance %s crashed", iid)
        fix = FixOutput(
            outcome="error", bug_id=iid, error=str(exc),
            final_state={"swebench_instance_id": iid, "model_patch": ""},
        )
    elapsed = time.monotonic() - t0

    fs = dict(fix.final_state or {})
    update_pred(preds_path, iid, model_name, fs.get("model_patch") or "")

    record = RunRecord.from_outputs(
        agent_name="mini_swe_agent",
        bug_id=iid,
        outcome=fix.outcome,
        error=fix.error,
        iterations=fix.iterations,
        final_state=fs,
        elapsed_s=round(elapsed, 3),
        agent_config={"llm_model": model_name},
        llm_model=model_name,
    )
    d = record.to_dict()
    (records_dir / f"{iid}.json").write_text(json.dumps(d, indent=2, default=str), encoding="utf-8")
    logger.info("done %s: outcome=%s iters=%d elapsed=%.1fs", iid, fix.outcome, fix.iterations, elapsed)
    return d


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the mini-swe-agent substrate over many SWE-bench instances.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--subset", default="verified", help="SWE-bench subset or dataset path (default: verified).")
    parser.add_argument("--split", default="test", help="Dataset split (default: test).")
    parser.add_argument("--instances", default="", help="Comma-separated instance ids (else the whole subset).")
    parser.add_argument("--instances-file", default="", help="File with one instance id per line (# comments ok).")
    parser.add_argument("--filter", dest="filter_re", default="", help="Regex over instance_id.")
    parser.add_argument("--slice", dest="slice_spec", default="", help="Slice, e.g. '0:10'.")
    parser.add_argument("--shuffle", action="store_true", help="Deterministic shuffle (seed 42) before slice.")
    parser.add_argument("--workers", type=int, default=4, help="Parallel agent workers (default: 4).")
    parser.add_argument("--grade-workers", type=int, default=4, help="Parallel harness workers (default: 4).")
    parser.add_argument("--config", action="append", default=[], help="mini config spec; repeatable.")
    parser.add_argument("--model", default=None, help="Override model name (else worker settings / config).")
    parser.add_argument("--run-id", default=None, help="Run id (default: timestamp). Reuse to resume.")
    parser.add_argument("--redo-existing", action="store_true", help="Re-run instances already in preds.json.")
    parser.add_argument("--no-grade", action="store_true", help="Skip harness grading (produce patches only).")
    args = parser.parse_args()

    instances = select_instances(
        args.subset, args.split, ids=[s for s in args.instances.split(",") if s.strip()],
        ids_file=args.instances_file, filter_re=args.filter_re,
        slice_spec=args.slice_spec, shuffle=args.shuffle,
    )
    if not instances:
        raise SystemExit("no instances selected")

    run_id = args.run_id or datetime.now(timezone.utc).strftime("swebench_%Y%m%dT%H%M%SZ")
    run_dir = _RUNS_ROOT / run_id
    records_dir = run_dir / "records"
    records_dir.mkdir(parents=True, exist_ok=True)
    preds_path = run_dir / "preds.json"

    existing: set[str] = set()
    if preds_path.exists() and not args.redo_existing:
        existing = set(json.loads(preds_path.read_text()).keys())
    todo = [i for i in instances if i["instance_id"] not in existing]

    mini_config = build_mini_config(args.config, args.model)
    model_name = (mini_config.get("model") or {}).get("model_name")

    logger.info(
        "run_dir=%s selected=%d todo=%d (skip %d done) workers=%d model=%s",
        run_dir, len(instances), len(todo), len(instances) - len(todo), args.workers, model_name,
    )

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(run_one, inst, mini_config, model_name, records_dir, preds_path): inst["instance_id"]
            for inst in todo
        }
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                logger.error("worker for %s failed: %s", futures[fut], exc)

    # Grade the whole selection (not just `todo`) so a resumed run re-grades all.
    selected_ids = [i["instance_id"] for i in instances]
    verdicts: dict[str, bool | None] = {}
    if not args.no_grade:
        verdicts = grade_with_harness(
            args.subset, args.split, selected_ids, preds_path,
            run_id=run_id, workdir=run_dir, grade_workers=args.grade_workers,
        )

    # Backfill resolved into each record + assemble summary.json (metrics input).
    summary: list[dict] = []
    for iid in selected_ids:
        rp = records_dir / f"{iid}.json"
        if not rp.exists():
            continue
        d = json.loads(rp.read_text())
        if iid in verdicts:
            d["resolved"] = verdicts[iid]
            rp.write_text(json.dumps(d, indent=2, default=str), encoding="utf-8")
        summary.append(d)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    n = len(summary)
    n_fixed = sum(1 for d in summary if d.get("outcome") == "fixed")
    n_resolved = sum(1 for d in summary if d.get("resolved") is True)
    n_graded = sum(1 for d in summary if d.get("resolved") is not None)
    results = {
        "run_id": run_id, "model": model_name, "n": n, "n_fixed": n_fixed,
        "n_resolved": n_resolved, "n_graded": n_graded,
        "resolved_rate": round(n_resolved / n, 3) if n else None,
    }
    (run_dir / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")

    print(f"\nrun_id        : {run_id}")
    print(f"instances     : {n}")
    print(f"fixed (patch) : {n_fixed}")
    print(f"resolved      : {n_resolved}" + ("" if args.no_grade else f" / {n_graded} graded"))
    print(f"resolved_rate : {results['resolved_rate']}")
    print(f"run dir       : {run_dir}")
    print(f"report        : python -m evaluation.cli report {run_id}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [swebench_batch %(name)s:%(funcName)s:%(lineno)d] %(message)s",
        stream=sys.stdout, force=True,
    )
    main()
