#!/usr/bin/env python3
"""
sweep.py — drive a set of SWE-bench instances through the ver==99 GitLab workflow.

For each instance it (idempotently) ensures the `instance/<id>` branch exists
(via setup_instance.build_and_push), then triggers that branch's baseline
pipeline. The failed baseline pipeline webhooks the gateway → orchestrator →
spawns a ver==99 worker → mini fixes it → auto/bf pipeline → validation routed
back → MR. The worker writes a journal RunRecord (with `resolved`) which this
driver reads to report the resolved-rate.

Requires the full stack (gateway + orchestrator + redis + ver==99 worker) to be
running and GitLab configured to webhook the gateway. This driver only triggers
pipelines and reads results — it does not run the worker itself.

Resumable: an instance whose journal already carries a terminal RunRecord is
skipped (unless --force). Credentials from env (GITLAB_API/GITLAB_PRIVATE_TOKEN),
never printed.

Usage:
    export $(grep -E '^(GITLAB_API|GITLAB_PRIVATE_TOKEN|GITLAB_USERNAME)=' \
             settings/worker_local_multi_process.env | xargs)
    python infra/swebench-gitlab/sweep.py --instances sympy__sympy-22914 sympy__sympy-23950
    python infra/swebench-gitlab/sweep.py --subset verified --repo sympy --limit 5
    python infra/swebench-gitlab/sweep.py --report-only --instances sympy__sympy-22914
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))

import setup_instance as si  # noqa: E402

JOURNAL_DIR = Path(os.environ.get("BF_JOURNAL_DIR") or (_ROOT / "evaluation" / "journal"))


# ── journal (resolved verdict) ────────────────────────────────────────────────

_TERMINAL = ("fixed", "no_fix", "error", "already_fixed")


def journal_verdict(instance_id: str, since: float = 0.0) -> dict | None:
    """Newest RunRecord for this instance, or None. Matches on
    swebench_instance_id (preferred) or bug_id containing the instance id.

    `since` (epoch seconds) ignores records older than it. Without it a re-run
    reads the PREVIOUS run's verdict the instant it triggers — the driver then
    "completes" every instance in milliseconds and reports stale results. The
    caller passes the moment it triggered the pipeline."""
    best = None
    best_mtime = since
    if not JOURNAL_DIR.exists():
        return None
    for rec_path in JOURNAL_DIR.glob("*/record.json"):
        try:
            mt = rec_path.stat().st_mtime
            if mt <= best_mtime:
                continue
            rec = json.loads(rec_path.read_text())
        except Exception:
            continue
        iid = rec.get("swebench_instance_id") or ""
        bug = rec.get("bug_id") or ""
        if iid != instance_id and instance_id not in bug:
            continue
        best, best_mtime = rec, mt
    return best


def is_done(instance_id: str, since: float = 0.0) -> bool:
    rec = journal_verdict(instance_id, since)
    return rec is not None and rec.get("outcome") in _TERMINAL


# ── GitLab trigger ────────────────────────────────────────────────────────────

def trigger_baseline_pipeline(instance: dict) -> str | None:
    api, tok = si._api()
    project = si.gitlab_project_path(instance["repo"])
    enc = urllib.parse.quote(project, safe="")
    ref = f"instance/{instance['instance_id']}"
    d = si._curl_json("POST", f"{api}/projects/{enc}/pipeline", tok, {"ref": ref})
    return str(d["id"]) if d.get("id") else None


# ── main ──────────────────────────────────────────────────────────────────────

def resolve_instances(args) -> list[dict]:
    if args.instances:
        # ONE dataset read for all ids (load_instance re-reads per call → 100
        # dataset loads for a 0:100 sweep).
        return si.load_instances(args.subset, args.instances)
    from datasets import load_dataset
    ds = load_dataset(si.DATASET_MAPPING.get(args.subset, args.subset), split="test")
    rows = [dict(x) for x in ds]
    if args.repo:
        rows = [r for r in rows if r["repo"].split("/")[-1] == args.repo]
    rows.sort(key=lambda r: r["instance_id"])
    if args.limit:
        rows = rows[: args.limit]
    return rows


def report(instances: list[dict]) -> None:
    total = len(instances)
    graded = resolved = 0
    print(f"\n=== resolved-rate report ({total} instance(s)) ===")
    for inst in instances:
        rec = journal_verdict(inst["instance_id"])
        if rec is None:
            print(f"  {inst['instance_id']:32s}  (no journal entry)")
            continue
        r = rec.get("resolved")
        graded += 1
        if r:
            resolved += 1
        print(f"  {inst['instance_id']:32s}  outcome={rec.get('outcome'):12s} resolved={r} "
              f"calls={rec.get('llm_call_count')} cost={rec.get('total_cost_usd')}")
    if graded:
        print(f"\nresolved: {resolved}/{graded} = {resolved/graded:.2%} (of graded); {total} requested")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instances", nargs="*", help="explicit instance_ids")
    ap.add_argument("--subset", default="verified")
    ap.add_argument("--repo", help="filter dataset to one upstream repo short-name (e.g. sympy)")
    ap.add_argument("--limit", type=int, help="cap number of instances")
    ap.add_argument("--force", action="store_true", help="re-run instances already in the journal")
    ap.add_argument("--report-only", action="store_true", help="just print the resolved-rate from the journal")
    ap.add_argument("--poll-timeout", type=int, default=1800, help="seconds to wait per instance for a journal verdict")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="instances in flight at once (default 1 = sequential). Each one costs a "
                         "worker process + a mini eval container (~2-4 GB) + a CI job container, so "
                         "size it against RAM, not cores.")
    ap.add_argument("--skip-setup", action="store_true",
                    help="assume the instance/<id> branches already exist (batch-prepped with "
                         "setup_instance.py --ci-skip); just trigger.")
    args = ap.parse_args()

    instances = resolve_instances(args)
    print(f"sweep: {len(instances)} instance(s), concurrency={args.concurrency}")

    if args.report_only:
        report(instances)
        return

    pending = list(instances)
    inflight: dict[str, tuple[dict, float, float]] = {}   # iid -> (inst, triggered_at, deadline)
    done = 0
    total = len(instances)

    while pending or inflight:
        # 1. fill the pipe
        while pending and len(inflight) < max(1, args.concurrency):
            inst = pending.pop(0)
            iid = inst["instance_id"]
            if not args.force and is_done(iid):
                print(f"[skip] {iid} — already has a journal verdict", flush=True)
                done += 1
                continue
            if not args.skip_setup:
                # ci_skip: the push must NOT auto-fire the baseline — we trigger it
                # ourselves below, under this driver's concurrency control
                # (see setup_instance.push_options).
                si.build_and_push(inst, ci_skip=True)     # idempotent branch
            # Record the trigger instant BEFORE triggering: any journal record
            # older than this belongs to a previous run, not to us.
            triggered_at = time.time()
            pipe = trigger_baseline_pipeline(inst)
            if not pipe:
                print(f"[trigger-fail] {iid}", flush=True)
                done += 1
                continue
            inflight[iid] = (inst, triggered_at, time.monotonic() + args.poll_timeout)
            print(f"[trigger] {iid} baseline pipeline={pipe} (inflight={len(inflight)})", flush=True)

        if not inflight:
            continue

        # 2. reap
        time.sleep(15)
        for iid in list(inflight):
            inst, triggered_at, deadline = inflight[iid]
            rec = journal_verdict(iid, since=triggered_at)
            if rec is not None and rec.get("outcome") in _TERMINAL:
                done += 1
                print(f"[{done}/{total}] {iid} outcome={rec.get('outcome')} "
                      f"resolved={rec.get('resolved')} elapsed_s={rec.get('elapsed_s')}", flush=True)
                del inflight[iid]
            elif time.monotonic() > deadline:
                done += 1
                print(f"[{done}/{total}] {iid} TIMEOUT after {args.poll_timeout}s", flush=True)
                del inflight[iid]

    report(instances)


if __name__ == "__main__":
    main()
