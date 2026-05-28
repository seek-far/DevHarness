#!/usr/bin/env python3
"""
tools/analyze_phase_log.py — turn phase_marker log lines into a stress-test report.

Reads one or more log files (gateway + orchestrator + worker stdout), greps for
``phase_marker`` lines, joins them across services by ``(job_id, ref)`` → bug_id
on the gateway→spawn_start hop and by ``bug_id`` everywhere downstream, then
prints per-bug latencies for the four phases plus aggregate p50/p95/max.

Phases:
  1. gateway_received  → spawn_start        (Redis stream queue + consumer poll)
  2. spawn_start       → worker_ready       (process/container startup + Python
                                             init + agent_ref reexec + LLM probe)
  3. fix_start         → fix_end            (agent.fix() wallclock — same number
                                             as RunRecord.elapsed_s × 1000)
  4. per llm_call wallclock_ms              (already on the line)

Optionally also reads a load_sampler TSV (--sampler) to report achieved
concurrency (max / mean / time-weighted mean of active_workers), max queue
depth, and heartbeat freshness percentiles.

Usage:
  python tools/analyze_phase_log.py --log gateway.log --log orchestrator.log
  python tools/analyze_phase_log.py --log "/tmp/burst-*.log" --sampler /tmp/burst.tsv
"""

from __future__ import annotations

import argparse
import glob
import re
import statistics
import sys
from pathlib import Path

# All phase_marker lines look like:
#   <log prefix> phase_marker phase=<X> key=value key=value ...
# Keep this regex permissive — log prefixes vary by service (gateway uses
# "[gw ...]", orchestrator uses "[orch ...]", worker uses "[worker:{bug_id} ...]")
# and a future refactor may change them.
_PHASE_MARKER_RE = re.compile(r"phase_marker\s+(.*)$")
# Each k=v pair: word, equals, then either an unquoted run of non-whitespace
# or a "quoted string with spaces" (we don't emit those today but keep the
# parser tolerant in case a future marker carries a ref like "feature/x y").
_KV_RE = re.compile(r"(\w+)=(\S+)")


def parse_markers(lines) -> list[dict]:
    """Return a list of {phase, k1: v1, ...} dicts, one per phase_marker line.

    Numeric-looking values (t_wall_ms, wallclock_ms, elapsed_ms, *tokens, *index,
    *count, iterations) are coerced to int so downstream stats math doesn't
    have to. Everything else stays a string.
    """
    out: list[dict] = []
    for line in lines:
        m = _PHASE_MARKER_RE.search(line)
        if not m:
            continue
        d: dict = {}
        for k, v in _KV_RE.findall(m.group(1)):
            if _is_intish(k):
                try:
                    d[k] = int(v)
                    continue
                except ValueError:
                    pass
            d[k] = v
        if "phase" in d:
            out.append(d)
    return out


def _is_intish(key: str) -> bool:
    suffixes = ("_ms", "_index", "_count", "_tokens")
    return key in ("iterations",) or any(key.endswith(s) for s in suffixes)


# ── per-bug timeline reconstruction ─────────────────────────────────────────


# Single-fire markers (one occurrence per bug). Stored as
# `bug["<phase>_t"] = t_wall_ms` so sub-phase deltas are trivial diffs.
# Each entry maps phase name → state key. Add new single-fire markers
# here and the analyzer picks them up — no extra branch needed.
_SINGLE_FIRE_PHASES = {
    "spawn_start":            "spawn_start_t",
    "worker_imports_done":    "worker_imports_done_t",
    "worker_agent_ref_done":  "worker_agent_ref_done_t",
    "worker_ready":           "worker_ready_t",
    "worker_llm_probe_done":  "worker_llm_probe_done_t",
    "fix_start":              "fix_start_t",
}


def _ensure_bug(bugs: dict, bug_id: str) -> dict:
    """Lazy-init a bug record with empty collections so callers don't have
    to `setdefault` each one. Order doesn't matter — markers can arrive
    in any sequence."""
    if bug_id not in bugs:
        bugs[bug_id] = {
            "llm_calls": [],
            "apply_test_attempts": [],
        }
    return bugs[bug_id]


def group_by_bug(markers: list[dict]) -> dict[str, dict]:
    """Build a per-bug timeline. Join gateway_received → spawn_start by
    (job_id, ref): gateway has no bug_id yet, orchestrator mints it on spawn.

    Returns: bug_id → {
        <phase>_t for each single-fire phase,
        fix_end_t / fix_elapsed_ms / outcome / iterations,
        ci_wait_start_t / ci_wait_end_t / ci_wait_elapsed_ms / ci_status,
        llm_calls: [{call_index, wallclock_ms, source}, ...],
        apply_test_attempts: [{attempt, start_t, venv_done_t, end_t,
                               elapsed_ms, test_passed, apply_error_present,
                               had_requirements}, ...],
    }
    """
    spawn_by_job: dict[tuple, str] = {}
    bugs: dict[str, dict] = {}

    # First pass: index spawn_start by (job_id, ref) so phase 1 can find its
    # peer; also seed the bug record + every single-fire phase ts.
    for m in markers:
        if m["phase"] == "spawn_start":
            bug_id = m.get("bug_id", "")
            key = (str(m.get("job_id", "")), str(m.get("ref", "")))
            spawn_by_job[key] = bug_id
            b = _ensure_bug(bugs, bug_id)
            b["spawn_start_t"] = m.get("t_wall_ms")
            b["job_id"] = key[0]
            b["ref"] = key[1]

    # Second pass: place every other marker onto its bug record.
    for m in markers:
        phase = m["phase"]
        if phase == "gateway_received":
            key = (str(m.get("job_id", "")), str(m.get("ref", "")))
            bug_id = spawn_by_job.get(key)
            if bug_id is None:
                # Webhook arrived but no spawn_start with the same (job_id, ref)
                # — either the orchestrator was down, or this run's logs are
                # incomplete. Skip rather than poison the stats with one-sided
                # data; the unmatched count is surfaced in the report.
                continue
            _ensure_bug(bugs, bug_id)["gateway_received_t"] = m.get("t_wall_ms")
        elif phase in _SINGLE_FIRE_PHASES and phase != "spawn_start":
            bug_id = m.get("bug_id", "")
            _ensure_bug(bugs, bug_id)[_SINGLE_FIRE_PHASES[phase]] = m.get("t_wall_ms")
        elif phase == "fix_end":
            bug_id = m.get("bug_id", "")
            b = _ensure_bug(bugs, bug_id)
            b["fix_end_t"] = m.get("t_wall_ms")
            b["fix_elapsed_ms"] = m.get("elapsed_ms")
            b["outcome"] = m.get("outcome", "")
            b["iterations"] = m.get("iterations", 0)
        elif phase == "llm_call":
            bug_id = m.get("bug_id", "")
            _ensure_bug(bugs, bug_id)["llm_calls"].append({
                "call_index": m.get("call_index", 0),
                "wallclock_ms": m.get("wallclock_ms", 0),
                "source": m.get("source", ""),
            })
        elif phase == "apply_test_start":
            bug_id = m.get("bug_id", "")
            _ensure_bug(bugs, bug_id)["apply_test_attempts"].append({
                "attempt": m.get("attempt", 0),
                "start_t": m.get("t_wall_ms"),
                "venv_done_t": None,
                "end_t": None,
                "elapsed_ms": None,
                "test_passed": None,
                "apply_error_present": None,
                "had_requirements": None,
            })
        elif phase == "apply_test_venv_done":
            bug_id = m.get("bug_id", "")
            attempts = _ensure_bug(bugs, bug_id)["apply_test_attempts"]
            target = _find_attempt(attempts, m.get("attempt", 0))
            if target is not None:
                target["venv_done_t"] = m.get("t_wall_ms")
                target["had_requirements"] = (m.get("had_requirements") == "True")
        elif phase == "apply_test_end":
            bug_id = m.get("bug_id", "")
            attempts = _ensure_bug(bugs, bug_id)["apply_test_attempts"]
            target = _find_attempt(attempts, m.get("attempt", 0))
            if target is not None:
                target["end_t"] = m.get("t_wall_ms")
                target["elapsed_ms"] = m.get("elapsed_ms")
                target["test_passed"] = (m.get("test_passed") == "True")
                target["apply_error_present"] = (m.get("apply_error_present") == "True")
        elif phase == "ci_wait_start":
            bug_id = m.get("bug_id", "")
            _ensure_bug(bugs, bug_id)["ci_wait_start_t"] = m.get("t_wall_ms")
        elif phase == "ci_wait_end":
            bug_id = m.get("bug_id", "")
            b = _ensure_bug(bugs, bug_id)
            b["ci_wait_end_t"] = m.get("t_wall_ms")
            b["ci_wait_elapsed_ms"] = m.get("elapsed_ms")
            b["ci_status"] = m.get("ci_status", "")
    return bugs


def _find_attempt(attempts: list[dict], attempt_idx) -> dict | None:
    """Pair _end / _venv_done markers to the _start marker that opened the
    same attempt. Match by `attempt` value (= fix_retry_count); fall back
    to "first unfinished" so a future marker that forgets to carry the
    attempt index still pairs sensibly."""
    for a in attempts:
        if a["attempt"] == attempt_idx and a.get("end_t") is None:
            return a
    for a in attempts:
        if a.get("end_t") is None:
            return a
    return None


def per_bug_phases(bug: dict) -> dict:
    """Compute the four phase durations from a bug's timeline. Missing
    markers leave the corresponding phase as None — the report shows '-'
    so a partial run is still readable.

    Phase 2 = spawn_start → fix_start (widened from the old "→ worker_ready"
    so that the LLM probe + make_agent costs are NOT silently attributed to
    phase 3). The sub-breakdown table then explains where inside phase 2 the
    time went. Falls back to worker_ready when fix_start is missing (partial
    run) so this stays useful for incomplete logs."""
    g = bug.get("gateway_received_t")
    s = bug.get("spawn_start_t")
    f0 = bug.get("fix_start_t")
    f1 = bug.get("fix_end_t")
    phase2_end = f0 if f0 is not None else bug.get("worker_ready_t")
    return {
        "phase1_ms": (s - g) if (g is not None and s is not None) else None,
        "phase2_ms": (phase2_end - s) if (s is not None and phase2_end is not None) else None,
        # Prefer the explicit elapsed_ms from the fix_end marker (same number
        # as RunRecord.elapsed_s × 1000) over re-deriving from t_wall_ms; the
        # latter would include event-loop scheduling slack.
        "phase3_ms": bug.get("fix_elapsed_ms") if bug.get("fix_elapsed_ms") is not None
                     else ((f1 - f0) if (f0 is not None and f1 is not None) else None),
    }


# ── aggregate stats ─────────────────────────────────────────────────────────


def _pct(values, q: float) -> float | None:
    """q in [0, 1]. Returns None for an empty list so the report can show '-'."""
    if not values:
        return None
    vs = sorted(values)
    if len(vs) == 1:
        return float(vs[0])
    # statistics.quantiles wants 0 < q < 1 with n=100 for percentiles; we just
    # do a direct index pick so 1-element + edge cases behave predictably.
    idx = min(len(vs) - 1, int(round(q * (len(vs) - 1))))
    return float(vs[idx])


def aggregate_phases(bugs: dict[str, dict]) -> dict:
    # per_bug_phases is the single source of truth for "what does phase N
    # equal for this bug" — call it once per bug here so the aggregate and
    # the per-row view never drift.
    phs = [per_bug_phases(b) for b in bugs.values()]
    p1 = [p["phase1_ms"] for p in phs if p["phase1_ms"] is not None]
    p2 = [p["phase2_ms"] for p in phs if p["phase2_ms"] is not None]
    p3 = [p["phase3_ms"] for p in phs if p["phase3_ms"] is not None]
    llm = [c["wallclock_ms"] for b in bugs.values() for c in b.get("llm_calls", [])]
    return {
        "phase1": _stats(p1),
        "phase2": _stats(p2),
        "phase3": _stats(p3),
        "llm_call": _stats(llm),
    }


def _stats(values) -> dict:
    return {
        "n": len(values),
        "p50": _pct(values, 0.5),
        "p95": _pct(values, 0.95),
        "max": max(values) if values else None,
    }


# Phase-2 sub-breakdown: consecutive single-fire marker pairs. Each entry
# is (label, from_key, to_key). The analyzer skips a sub-phase for any
# bug missing either endpoint, so a partial run (e.g. worker never made
# it past worker_imports_done) still reports the prefix.
_PHASE2_SUBPHASES = [
    ("cold_python",       "spawn_start_t",           "worker_imports_done_t"),
    ("agent_ref_reexec",  "worker_imports_done_t",   "worker_agent_ref_done_t"),
    ("asyncio+redis",     "worker_agent_ref_done_t", "worker_ready_t"),
    ("llm_probe",         "worker_ready_t",          "worker_llm_probe_done_t"),
    ("make_agent",        "worker_llm_probe_done_t", "fix_start_t"),
]


def aggregate_phase2_subphases(bugs: dict[str, dict]) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for label, from_k, to_k in _PHASE2_SUBPHASES:
        vals = []
        for b in bugs.values():
            a = b.get(from_k)
            z = b.get(to_k)
            if a is not None and z is not None and z >= a:
                vals.append(z - a)
        out.append((label, _stats(vals)))
    return out


def aggregate_phase3_subphases(bugs: dict[str, dict]) -> list[tuple[str, dict]]:
    """Phase-3 has TWO repeatable inner sub-intervals (apply_test fires once
    per retry; ci_wait fires once per bug today but the analyzer treats it
    as single anyway). For apply_test we report per-attempt p50/p95 (so the
    "single attempt" cost is visible) AND per-bug sums (so the "total time
    spent in this node across retries" is visible)."""
    apply_test_per_attempt: list[int] = []
    venv_install_per_attempt: list[int] = []
    pytest_per_attempt: list[int] = []
    apply_test_per_bug_sum: list[int] = []
    ci_wait: list[int] = []
    attempts_per_bug: list[int] = []

    for b in bugs.values():
        bug_sum = 0
        bug_has_any = False
        attempts = b.get("apply_test_attempts", [])
        for a in attempts:
            if a.get("elapsed_ms") is not None:
                apply_test_per_attempt.append(a["elapsed_ms"])
                bug_sum += a["elapsed_ms"]
                bug_has_any = True
            if a.get("start_t") is not None and a.get("venv_done_t") is not None:
                venv_install_per_attempt.append(a["venv_done_t"] - a["start_t"])
            if (a.get("venv_done_t") is not None
                    and a.get("end_t") is not None
                    and a["end_t"] >= a["venv_done_t"]):
                pytest_per_attempt.append(a["end_t"] - a["venv_done_t"])
        if bug_has_any:
            apply_test_per_bug_sum.append(bug_sum)
            attempts_per_bug.append(len(attempts))
        if b.get("ci_wait_elapsed_ms") is not None:
            ci_wait.append(b["ci_wait_elapsed_ms"])

    return [
        ("apply_test (per attempt)",   _stats(apply_test_per_attempt)),
        ("  └ venv+pip install",       _stats(venv_install_per_attempt)),
        ("  └ pytest run",             _stats(pytest_per_attempt)),
        ("apply_test (per-bug sum)",   _stats(apply_test_per_bug_sum)),
        ("attempts per bug",           _stats(attempts_per_bug)),
        ("ci_wait (per bug)",          _stats(ci_wait)),
    ]


# ── load_sampler TSV ────────────────────────────────────────────────────────


def parse_sampler_tsv(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as fp:
        header = fp.readline().rstrip("\n").split("\t")
        for line in fp:
            cells = line.rstrip("\n").split("\t")
            if len(cells) != len(header):
                continue
            row = dict(zip(header, cells))
            for k in ("t_wall_ms", "queue_depth", "active_workers"):
                if k in row and row[k] != "":
                    try:
                        row[k] = int(row[k])
                    except ValueError:
                        row[k] = None
            for k in ("hb_age_ms_p50", "hb_age_ms_max"):
                if k in row and row[k] != "":
                    try:
                        row[k] = int(row[k])
                    except ValueError:
                        row[k] = None
                else:
                    row[k] = None
            rows.append(row)
    return rows


def sampler_summary(rows: list[dict]) -> dict:
    """Achieved concurrency + queue + heartbeat stats from the TSV.

    Achieved concurrency intentionally distinguishes "max" (peak parallel
    workers, the burst metric) from "time-weighted mean" (the average
    in-flight count weighted by inter-sample interval — closer to "effective
    parallelism" than a simple sample-mean when sampling cadence varies).
    """
    aw = [r["active_workers"] for r in rows if isinstance(r.get("active_workers"), int)]
    qd = [r["queue_depth"] for r in rows if isinstance(r.get("queue_depth"), int)]
    hb_p50 = [r["hb_age_ms_p50"] for r in rows if isinstance(r.get("hb_age_ms_p50"), int)]
    hb_max = [r["hb_age_ms_max"] for r in rows if isinstance(r.get("hb_age_ms_max"), int)]

    tw_mean = None
    if len(rows) >= 2:
        total_dt = 0
        weighted = 0
        for prev, nxt in zip(rows[:-1], rows[1:]):
            t_prev = prev.get("t_wall_ms")
            t_nxt = nxt.get("t_wall_ms")
            a = prev.get("active_workers")
            if isinstance(t_prev, int) and isinstance(t_nxt, int) and isinstance(a, int):
                dt = max(0, t_nxt - t_prev)
                total_dt += dt
                weighted += a * dt
        tw_mean = (weighted / total_dt) if total_dt > 0 else None

    return {
        "samples": len(rows),
        "active_workers_max": max(aw) if aw else None,
        "active_workers_mean": (statistics.mean(aw) if aw else None),
        "active_workers_time_weighted_mean": tw_mean,
        "queue_depth_max": max(qd) if qd else None,
        "queue_depth_mean": (statistics.mean(qd) if qd else None),
        "hb_age_ms_p50": int(statistics.median(hb_p50)) if hb_p50 else None,
        "hb_age_ms_max": max(hb_max) if hb_max else None,
    }


# ── report ──────────────────────────────────────────────────────────────────


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v)


def render_report(bugs: dict[str, dict], agg: dict, sampler: dict | None) -> str:
    lines: list[str] = []
    lines.append("=== Per-bug latency breakdown (ms) ===")
    lines.append(f"{'bug_id':40s} {'phase1':>8s} {'phase2':>8s} {'phase3':>8s} "
                 f"{'llm_calls':>9s} {'llm_p95':>8s} {'outcome':>10s}")
    for bug_id in sorted(bugs):
        b = bugs[bug_id]
        ph = per_bug_phases(b)
        llm_ms = [c["wallclock_ms"] for c in b.get("llm_calls", [])]
        n_calls = len(llm_ms)
        llm_p95 = _pct(llm_ms, 0.95)
        lines.append(
            f"{bug_id:40s} {_fmt(ph['phase1_ms']):>8s} {_fmt(ph['phase2_ms']):>8s} "
            f"{_fmt(ph['phase3_ms']):>8s} {n_calls:>9d} {_fmt(llm_p95):>8s} "
            f"{_fmt(b.get('outcome')):>10s}"
        )
    lines.append("")
    lines.append("=== Aggregate stats (ms) ===")
    lines.append(f"N bugs: {len(bugs)}")
    for name, label in (
        ("phase1", "gateway → spawn"),
        ("phase2", "spawn → fix_start"),  # widened to include the LLM probe gap
        ("phase3", "fix() wallclock"),
        ("llm_call", "per LLM call"),
    ):
        s = agg[name]
        lines.append(
            f"{name:9s} {label:24s} n={s['n']:>4d}  "
            f"p50={_fmt(s['p50']):>8s}  p95={_fmt(s['p95']):>8s}  "
            f"max={_fmt(s['max']):>8s}"
        )
    lines.append("")
    lines.append("=== Phase-2 sub-breakdown (ms; spawn_start → fix_start) ===")
    for label, s in aggregate_phase2_subphases(bugs):
        lines.append(
            f"  {label:22s} n={s['n']:>4d}  "
            f"p50={_fmt(s['p50']):>8s}  p95={_fmt(s['p95']):>8s}  "
            f"max={_fmt(s['max']):>8s}"
        )
    lines.append("")
    lines.append("=== Phase-3 sub-breakdown (ms; inside fix()) ===")
    for label, s in aggregate_phase3_subphases(bugs):
        lines.append(
            f"  {label:28s} n={s['n']:>4d}  "
            f"p50={_fmt(s['p50']):>8s}  p95={_fmt(s['p95']):>8s}  "
            f"max={_fmt(s['max']):>8s}"
        )
    if sampler is not None:
        lines.append("")
        lines.append("=== Achieved concurrency (from sampler) ===")
        lines.append(f"samples: {sampler['samples']}")
        lines.append(
            f"active_workers   max={_fmt(sampler['active_workers_max']):>5s}  "
            f"mean={_fmt(sampler['active_workers_mean']):>6s}  "
            f"time_weighted_mean={_fmt(sampler['active_workers_time_weighted_mean']):>6s}"
        )
        lines.append(
            f"queue_depth      max={_fmt(sampler['queue_depth_max']):>5s}  "
            f"mean={_fmt(sampler['queue_depth_mean']):>6s}"
        )
        lines.append(
            f"hb_age_ms        p50={_fmt(sampler['hb_age_ms_p50']):>5s}  "
            f"max={_fmt(sampler['hb_age_ms_max']):>6s}"
        )
    return "\n".join(lines) + "\n"


# ── CLI ─────────────────────────────────────────────────────────────────────


def _read_log_files(patterns: list[str]):
    for pat in patterns:
        for path in sorted(glob.glob(pat)) or [pat]:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fp:
                    yield from fp
            except OSError as e:
                print(f"# warning: could not read {path}: {e}", file=sys.stderr)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--log", action="append", default=[], required=False,
                   help="path or glob of a service log file; repeatable")
    p.add_argument("--sampler", type=Path, default=None,
                   help="load_sampler TSV (optional)")
    p.add_argument("--out", type=Path, default=None,
                   help="write report to file (default: stdout)")
    args = p.parse_args(argv)

    if not args.log:
        p.error("at least one --log is required")

    markers = parse_markers(_read_log_files(args.log))
    bugs = group_by_bug(markers)
    agg = aggregate_phases(bugs)
    sampler = sampler_summary(parse_sampler_tsv(args.sampler)) if args.sampler else None

    report = render_report(bugs, agg, sampler)
    if args.out:
        args.out.write_text(report, encoding="utf-8")
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
