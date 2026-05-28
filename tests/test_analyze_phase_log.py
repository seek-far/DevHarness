"""Tests for tools/analyze_phase_log.py.

Synthetic phase_marker lines (the same shape the three services emit at
runtime) fed through the parser → join → aggregate pipeline. Cover the
join asymmetry: gateway has no bug_id yet, so phase 1 is tied to
spawn_start by (job_id, ref); everything else joins by bug_id.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _line(prefix: str, payload: str) -> str:
    # Mimic the runtime log shape — the analyzer only cares about the
    # phase_marker substring, but tests should look like real lines so a
    # future format tweak (e.g. JSON logger) shows up here too.
    return f"2026-05-28 10:00:00 INFO [{prefix}] {payload}\n"


# ── parse + group ───────────────────────────────────────────────────────────


def test_parse_markers_extracts_kv_and_coerces_ints():
    from tools import analyze_phase_log as mod

    lines = [
        _line("gw gateway:webhook:60",
              "phase_marker phase=gateway_received job_id=99 ref=main t_wall_ms=1000"),
        _line("orch orchestrator:_handle_message:120",
              "phase_marker phase=spawn_start bug_id=BUG-A job_id=99 ref=main t_wall_ms=1100"),
    ]
    markers = mod.parse_markers(lines)
    assert len(markers) == 2
    assert markers[0]["phase"] == "gateway_received"
    # Time fields coerce to int (we do math on them); job_id stays a string
    # because the analyzer uses it as part of the (job_id, ref) join key —
    # gateway emits the GitLab build id verbatim, orchestrator wraps it in
    # str() already, so the string form is what flows through everywhere.
    assert markers[0]["t_wall_ms"] == 1000
    assert markers[0]["job_id"] == "99"
    assert markers[1]["bug_id"] == "BUG-A"


def test_group_by_bug_joins_gateway_to_spawn_by_job_ref():
    """Phase 1 is the only hop where bug_id doesn't yet exist on the source
    line — must be reconstructed from spawn_start's (job_id, ref)."""
    from tools import analyze_phase_log as mod

    markers = mod.parse_markers([
        _line("gw", "phase_marker phase=gateway_received job_id=99 ref=main t_wall_ms=1000"),
        _line("orch", "phase_marker phase=spawn_start bug_id=BUG-A job_id=99 ref=main t_wall_ms=1100"),
        _line("worker", "phase_marker phase=worker_ready bug_id=BUG-A t_wall_ms=1400"),
        _line("worker", "phase_marker phase=fix_start bug_id=BUG-A t_wall_ms=1450"),
        _line("worker", "phase_marker phase=fix_end bug_id=BUG-A outcome=fixed iterations=0 "
                       "elapsed_ms=8500 t_wall_ms=9950"),
    ])
    bugs = mod.group_by_bug(markers)
    assert set(bugs) == {"BUG-A"}
    ph = mod.per_bug_phases(bugs["BUG-A"])
    assert ph["phase1_ms"] == 100        # 1100 - 1000
    # Phase 2 = spawn_start → fix_start (widened to capture the LLM probe
    # gap between worker_ready and fix_start, which used to fall in no
    # phase at all).
    assert ph["phase2_ms"] == 350        # 1450 - 1100
    assert ph["phase3_ms"] == 8500       # explicit elapsed_ms wins over wall delta


def test_group_by_bug_handles_concurrent_bugs_independently():
    """Two interleaved bugs — joins must not bleed across (job_id, ref) pairs."""
    from tools import analyze_phase_log as mod

    markers = mod.parse_markers([
        _line("gw", "phase_marker phase=gateway_received job_id=10 ref=main t_wall_ms=1000"),
        _line("gw", "phase_marker phase=gateway_received job_id=11 ref=feature/x t_wall_ms=1005"),
        _line("orch", "phase_marker phase=spawn_start bug_id=BUG-X job_id=11 ref=feature/x t_wall_ms=1110"),
        _line("orch", "phase_marker phase=spawn_start bug_id=BUG-A job_id=10 ref=main t_wall_ms=1100"),
        _line("worker", "phase_marker phase=worker_ready bug_id=BUG-A t_wall_ms=1450"),
        _line("worker", "phase_marker phase=worker_ready bug_id=BUG-X t_wall_ms=1430"),
    ])
    bugs = mod.group_by_bug(markers)
    assert mod.per_bug_phases(bugs["BUG-A"])["phase1_ms"] == 100
    assert mod.per_bug_phases(bugs["BUG-X"])["phase1_ms"] == 105
    assert mod.per_bug_phases(bugs["BUG-A"])["phase2_ms"] == 350
    assert mod.per_bug_phases(bugs["BUG-X"])["phase2_ms"] == 320


def test_unmatched_gateway_received_does_not_corrupt_stats():
    """A gateway line that never got a spawn (orchestrator down, partial logs)
    must be silently dropped — better to under-report than to land in some
    bogus bucket and skew p95."""
    from tools import analyze_phase_log as mod

    markers = mod.parse_markers([
        _line("gw", "phase_marker phase=gateway_received job_id=99 ref=main t_wall_ms=1000"),
    ])
    bugs = mod.group_by_bug(markers)
    assert bugs == {}


def test_llm_calls_accumulate_per_bug():
    from tools import analyze_phase_log as mod

    markers = mod.parse_markers([
        _line("orch", "phase_marker phase=spawn_start bug_id=BUG-A job_id=1 ref=main t_wall_ms=1000"),
        _line("worker", "phase_marker phase=llm_call bug_id=BUG-A source=react_loop "
                       "call_index=1 wallclock_ms=900 prompt_tokens=500 completion_tokens=20 t_wall_ms=2000"),
        _line("worker", "phase_marker phase=llm_call bug_id=BUG-A source=react_loop "
                       "call_index=2 wallclock_ms=1200 prompt_tokens=600 completion_tokens=30 t_wall_ms=3300"),
        _line("worker", "phase_marker phase=llm_call bug_id=BUG-A source=reflection "
                       "call_index=3 wallclock_ms=400 prompt_tokens=200 completion_tokens=15 t_wall_ms=3800"),
    ])
    bugs = mod.group_by_bug(markers)
    calls = bugs["BUG-A"]["llm_calls"]
    assert len(calls) == 3
    assert [c["wallclock_ms"] for c in calls] == [900, 1200, 400]
    assert [c["source"] for c in calls] == ["react_loop", "react_loop", "reflection"]


# ── aggregate ───────────────────────────────────────────────────────────────


def test_aggregate_phases_reports_per_phase_percentiles():
    from tools import analyze_phase_log as mod

    # Build three bugs with hand-picked phase values so the percentiles are
    # easy to read. fix_start is placed exactly p2 ms after spawn_start so
    # the widened phase-2 (spawn_start → fix_start) equals p2 directly.
    markers = []
    for bug_id, p1, p2, p3 in [
        ("BUG-A", 10, 100, 1000),
        ("BUG-B", 50, 200, 2000),
        ("BUG-C", 90, 300, 3000),
    ]:
        t = 1000
        markers.extend(mod.parse_markers([
            _line("gw", f"phase_marker phase=gateway_received job_id={bug_id} ref=main t_wall_ms={t}"),
            _line("orch", f"phase_marker phase=spawn_start bug_id={bug_id} job_id={bug_id} ref=main "
                          f"t_wall_ms={t + p1}"),
            _line("worker", f"phase_marker phase=fix_start bug_id={bug_id} t_wall_ms={t + p1 + p2}"),
            _line("worker", f"phase_marker phase=fix_end bug_id={bug_id} outcome=fixed iterations=0 "
                            f"elapsed_ms={p3} t_wall_ms={t + p1 + p2 + p3}"),
        ]))
    bugs = mod.group_by_bug(markers)
    agg = mod.aggregate_phases(bugs)

    assert agg["phase1"]["n"] == 3
    assert agg["phase1"]["p50"] == 50
    assert agg["phase1"]["max"] == 90
    assert agg["phase2"]["p50"] == 200
    assert agg["phase3"]["p50"] == 2000
    # No LLM calls in this fixture — observability stays sane on the empty path.
    assert agg["llm_call"]["n"] == 0
    assert agg["llm_call"]["p50"] is None


# ── sampler TSV ─────────────────────────────────────────────────────────────


def test_sampler_summary_time_weighted_mean(tmp_path):
    """Time-weighted mean uses inter-sample interval as weight, which differs
    from the simple sample-mean when active_workers changes mid-window. A
    burst that's at 4 workers for 200 ms then 0 for 800 ms should report
    time-weighted mean ≈ 0.8, not 2.0 (the naive sample mean of [4, 0])."""
    from tools import analyze_phase_log as mod

    tsv = tmp_path / "s.tsv"
    tsv.write_text(
        "t_wall_ms\tqueue_depth\tactive_workers\tactive_workers_csv\thb_age_ms_p50\thb_age_ms_max\n"
        "1000\t1\t4\tBUG-A,BUG-B,BUG-C,BUG-D\t100\t200\n"
        "1200\t0\t0\t\t\t\n"
        "2000\t0\t0\t\t\t\n",
        encoding="utf-8",
    )
    rows = mod.parse_sampler_tsv(tsv)
    summary = mod.sampler_summary(rows)
    assert summary["samples"] == 3
    assert summary["active_workers_max"] == 4
    # Weighted: 4 * 200 ms + 0 * 800 ms = 800 active-worker-ms over 1000 ms
    # ≈ 0.8 mean concurrency — the right way to talk about achieved parallelism.
    assert summary["active_workers_time_weighted_mean"] == 0.8


# ── render ──────────────────────────────────────────────────────────────────


# ── phase 2 sub-markers ─────────────────────────────────────────────────────


def test_phase2_subbreakdown_deltas_each_pair():
    """The five worker-startup sub-markers must each show up as a labelled
    sub-phase with the delta from the prior marker."""
    from tools import analyze_phase_log as mod

    markers = mod.parse_markers([
        _line("orch", "phase_marker phase=spawn_start bug_id=BUG-A job_id=1 ref=main t_wall_ms=1000"),
        _line("worker", "phase_marker phase=worker_imports_done bug_id=BUG-A t_wall_ms=3500"),
        _line("worker", "phase_marker phase=worker_agent_ref_done bug_id=BUG-A t_wall_ms=3510"),
        _line("worker", "phase_marker phase=worker_ready bug_id=BUG-A t_wall_ms=3600"),
        _line("worker", "phase_marker phase=worker_llm_probe_done bug_id=BUG-A t_wall_ms=4800"),
        _line("worker", "phase_marker phase=fix_start bug_id=BUG-A t_wall_ms=4810"),
    ])
    bugs = mod.group_by_bug(markers)
    sub = dict(mod.aggregate_phase2_subphases(bugs))
    assert sub["cold_python"]["p50"] == 2500          # 3500 - 1000
    assert sub["agent_ref_reexec"]["p50"] == 10       # 3510 - 3500
    assert sub["asyncio+redis"]["p50"] == 90          # 3600 - 3510
    assert sub["llm_probe"]["p50"] == 1200            # 4800 - 3600
    assert sub["make_agent"]["p50"] == 10             # 4810 - 4800


def test_phase2_subbreakdown_skips_bugs_missing_endpoints():
    """A bug whose worker died before logging worker_llm_probe_done MUST NOT
    poison the llm_probe sub-phase stats — it should just be skipped."""
    from tools import analyze_phase_log as mod

    markers = mod.parse_markers([
        # Bug with full markers
        _line("orch", "phase_marker phase=spawn_start bug_id=BUG-A job_id=1 ref=main t_wall_ms=1000"),
        _line("worker", "phase_marker phase=worker_imports_done bug_id=BUG-A t_wall_ms=3000"),
        _line("worker", "phase_marker phase=worker_agent_ref_done bug_id=BUG-A t_wall_ms=3005"),
        _line("worker", "phase_marker phase=worker_ready bug_id=BUG-A t_wall_ms=3100"),
        _line("worker", "phase_marker phase=worker_llm_probe_done bug_id=BUG-A t_wall_ms=4000"),
        # Bug that died after imports — no llm_probe_done
        _line("orch", "phase_marker phase=spawn_start bug_id=BUG-B job_id=2 ref=main t_wall_ms=1000"),
        _line("worker", "phase_marker phase=worker_imports_done bug_id=BUG-B t_wall_ms=3200"),
        _line("worker", "phase_marker phase=worker_agent_ref_done bug_id=BUG-B t_wall_ms=3205"),
        _line("worker", "phase_marker phase=worker_ready bug_id=BUG-B t_wall_ms=3300"),
    ])
    bugs = mod.group_by_bug(markers)
    sub = dict(mod.aggregate_phase2_subphases(bugs))
    # cold_python and asyncio+redis sub-phases see both bugs.
    assert sub["cold_python"]["n"] == 2
    assert sub["asyncio+redis"]["n"] == 2
    # llm_probe only sees BUG-A (BUG-B died before worker_llm_probe_done).
    assert sub["llm_probe"]["n"] == 1
    assert sub["llm_probe"]["p50"] == 900  # 4000 - 3100


# ── phase 3 sub-markers ─────────────────────────────────────────────────────


def test_phase3_subbreakdown_apply_test_pairs_attempts():
    """apply_test_start/end can fire multiple times per bug (one per
    fix_retry). The analyzer must pair them by `attempt` so the per-attempt
    elapsed_ms is correct and the per-bug sum adds across all attempts."""
    from tools import analyze_phase_log as mod

    markers = mod.parse_markers([
        _line("orch", "phase_marker phase=spawn_start bug_id=BUG-A job_id=1 ref=main t_wall_ms=1000"),
        # First attempt — apply failed
        _line("worker", "phase_marker phase=apply_test_start bug_id=BUG-A attempt=0 t_wall_ms=5000"),
        _line("worker", "phase_marker phase=apply_test_end bug_id=BUG-A attempt=0 test_passed=False "
                       "apply_error_present=True elapsed_ms=500 t_wall_ms=5500"),
        # Second attempt — patch applied, venv installed, tests passed
        _line("worker", "phase_marker phase=apply_test_start bug_id=BUG-A attempt=1 t_wall_ms=10000"),
        _line("worker", "phase_marker phase=apply_test_venv_done bug_id=BUG-A attempt=1 "
                       "had_requirements=True t_wall_ms=12500"),
        _line("worker", "phase_marker phase=apply_test_end bug_id=BUG-A attempt=1 test_passed=True "
                       "apply_error_present=False elapsed_ms=4500 t_wall_ms=14500"),
    ])
    bugs = mod.group_by_bug(markers)
    sub = dict(mod.aggregate_phase3_subphases(bugs))
    # Per-attempt: both attempts contribute one elapsed_ms each (500, 4500).
    assert sub["apply_test (per attempt)"]["n"] == 2
    assert sub["apply_test (per attempt)"]["max"] == 4500
    # Per-bug sum: 500 + 4500 = 5000 across the single bug.
    assert sub["apply_test (per-bug sum)"]["n"] == 1
    assert sub["apply_test (per-bug sum)"]["max"] == 5000
    # Only attempt 1 has venv+pytest split — attempt 0 rejected before venv.
    assert sub["  └ venv+pip install"]["n"] == 1
    assert sub["  └ venv+pip install"]["max"] == 2500   # 12500 - 10000
    assert sub["  └ pytest run"]["n"] == 1
    assert sub["  └ pytest run"]["max"] == 2000         # 14500 - 12500


def test_phase3_subbreakdown_ci_wait_per_bug():
    from tools import analyze_phase_log as mod

    markers = mod.parse_markers([
        _line("orch", "phase_marker phase=spawn_start bug_id=BUG-A job_id=1 ref=main t_wall_ms=1000"),
        _line("worker", "phase_marker phase=ci_wait_start bug_id=BUG-A t_wall_ms=20000"),
        _line("worker", "phase_marker phase=ci_wait_end bug_id=BUG-A ci_status=success retries=0 "
                       "elapsed_ms=45000 t_wall_ms=65000"),
    ])
    bugs = mod.group_by_bug(markers)
    sub = dict(mod.aggregate_phase3_subphases(bugs))
    assert sub["ci_wait (per bug)"]["n"] == 1
    assert sub["ci_wait (per bug)"]["max"] == 45000


# ── render ──────────────────────────────────────────────────────────────────


def test_render_report_includes_all_sections(tmp_path):
    from tools import analyze_phase_log as mod

    markers = mod.parse_markers([
        _line("gw", "phase_marker phase=gateway_received job_id=1 ref=main t_wall_ms=1000"),
        _line("orch", "phase_marker phase=spawn_start bug_id=BUG-A job_id=1 ref=main t_wall_ms=1100"),
        _line("worker", "phase_marker phase=worker_ready bug_id=BUG-A t_wall_ms=1400"),
        _line("worker", "phase_marker phase=fix_start bug_id=BUG-A t_wall_ms=1450"),
        _line("worker", "phase_marker phase=llm_call bug_id=BUG-A source=react_loop "
                       "call_index=1 wallclock_ms=900 prompt_tokens=500 completion_tokens=20 t_wall_ms=2000"),
        _line("worker", "phase_marker phase=fix_end bug_id=BUG-A outcome=fixed iterations=0 "
                       "elapsed_ms=8500 t_wall_ms=9950"),
    ])
    bugs = mod.group_by_bug(markers)
    agg = mod.aggregate_phases(bugs)

    tsv = tmp_path / "s.tsv"
    tsv.write_text(
        "t_wall_ms\tqueue_depth\tactive_workers\tactive_workers_csv\thb_age_ms_p50\thb_age_ms_max\n"
        "1000\t1\t1\tBUG-A\t100\t200\n",
        encoding="utf-8",
    )
    sampler = mod.sampler_summary(mod.parse_sampler_tsv(tsv))

    report = mod.render_report(bugs, agg, sampler)
    assert "Per-bug latency breakdown" in report
    assert "BUG-A" in report
    assert "Aggregate stats" in report
    assert "Phase-2 sub-breakdown" in report
    assert "Phase-3 sub-breakdown" in report
    assert "Achieved concurrency" in report
    assert "active_workers" in report
