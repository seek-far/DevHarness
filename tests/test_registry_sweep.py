"""WorkerRegistry done/failed entry cleanup tests.

Background: pre-2026-05-29 the orchestrator left every done/failed
WorkerEntry in `_workers` forever — `remove()` existed but no caller
invoked it. Across a long-running orchestrator process the dict grew
unboundedly (verified live: terminal entries stayed visible despite
fix_end having fired).

Fix shape:
  * WorkerEntry gains `done_at: float | None`, stamped on the first
    `update_status(bug_id, "done"|"failed")` call.
  * WorkerRegistry.sweep_stale(grace_seconds) walks `_workers`,
    deletes entries with terminal status whose `done_at` is older
    than `grace_seconds`.
  * HealthMonitor calls sweep_stale at the start of each
    `_check_all` tick (cheap; n stays small because of the sweep).

The grace exists so a late ValidationStatusEvent (CI for the
fix-branch a just-exited worker pushed) can still find bug_id in
the registry, logging a meaningful "no active worker" instead of
silently dropping.

These tests pin the lifecycle contract end to end.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.models import WARMUP_GRACE, WorkerEntry
from orchestrator.monitor import HealthMonitor
from orchestrator.registry import WorkerRegistry


def _entry(bug_id: str, status: str = "running",
           done_at: float | None = None) -> WorkerEntry:
    return WorkerEntry(
        bug_id=bug_id, process=MagicMock(returncode=None),
        project_id="1", project_web_url="u", job_id="j",
        started_at=time.time(),
        warmup_deadline=time.time() + WARMUP_GRACE,
        status=status, done_at=done_at,
    )


# ── update_status stamps done_at on the right transitions ───────────────────


def test_update_status_to_done_sets_done_at():
    r = WorkerRegistry()
    r.register(_entry("b1", status="running"))
    before = time.time()
    r.update_status("b1", "done")
    after = time.time()
    e = r.get("b1")
    assert e.status == "done"
    assert e.done_at is not None
    assert before - 0.1 <= e.done_at <= after + 0.1


def test_update_status_to_failed_sets_done_at():
    r = WorkerRegistry()
    r.register(_entry("b1", status="running"))
    r.update_status("b1", "failed")
    assert r.get("b1").done_at is not None


def test_update_status_to_running_does_not_set_done_at():
    """Non-terminal transitions must leave done_at alone — otherwise
    a worker that was transiently marked done then resurrected would
    be mis-aged."""
    r = WorkerRegistry()
    r.register(_entry("b1", status="warmup"))
    r.update_status("b1", "running")
    assert r.get("b1").done_at is None


def test_terminal_repeat_does_not_reset_done_at():
    """Idempotency: a second update_status(done) MUST keep the
    original done_at so the grace measures age since first observed
    terminal, not since last touch."""
    r = WorkerRegistry()
    r.register(_entry("b1", status="running"))
    r.update_status("b1", "done")
    original = r.get("b1").done_at
    time.sleep(0.05)
    r.update_status("b1", "done")   # second call — same status
    assert r.get("b1").done_at == original


# ── sweep_stale behaviour ───────────────────────────────────────────────────


def test_sweep_removes_terminal_entries_past_grace():
    r = WorkerRegistry()
    # Manually plant an entry with an old done_at to skip waiting.
    old = _entry("b1", status="done", done_at=time.time() - 100)
    r._workers["b1"] = old
    removed = r.sweep_stale(grace_seconds=30)
    assert removed == 1
    assert r.get("b1") is None


def test_sweep_leaves_terminal_entries_under_grace():
    """Just-terminal entries must survive — a late
    ValidationStatusEvent should still find them by bug_id during
    the grace window."""
    r = WorkerRegistry()
    fresh = _entry("b1", status="done", done_at=time.time() - 5)
    r._workers["b1"] = fresh
    removed = r.sweep_stale(grace_seconds=60)
    assert removed == 0
    assert r.get("b1") is not None


def test_sweep_leaves_active_entries_alone_even_if_old():
    """`done_at` is the only age signal. An entry with status=running
    and no done_at MUST never be swept regardless of when it was
    registered — sweep is for terminal cleanup, not idle workers."""
    r = WorkerRegistry()
    old_running = _entry("b1", status="running")
    old_running.started_at = time.time() - 86400   # 1 day old
    r._workers["b1"] = old_running
    removed = r.sweep_stale(grace_seconds=30)
    assert removed == 0
    assert r.get("b1") is not None


def test_sweep_handles_mixed_population():
    r = WorkerRegistry()
    now = time.time()
    r._workers["fresh_done"] = _entry("fresh_done", "done",
                                       done_at=now - 5)
    r._workers["old_done"]   = _entry("old_done",   "done",
                                       done_at=now - 200)
    r._workers["old_failed"] = _entry("old_failed", "failed",
                                       done_at=now - 200)
    r._workers["running"]    = _entry("running",    "running")
    removed = r.sweep_stale(grace_seconds=30)
    assert removed == 2
    assert {k for k in r._workers} == {"fresh_done", "running"}


def test_sweep_skips_terminal_without_done_at():
    """Defensive: if a future refactor sets status=done without going
    through update_status() (bypassing the done_at stamp), the entry
    must NOT be swept — better a leak than dropping an entry whose
    age we can't bound."""
    r = WorkerRegistry()
    weird = _entry("b1", status="done", done_at=None)
    r._workers["b1"] = weird
    removed = r.sweep_stale(grace_seconds=0.001)
    assert removed == 0
    assert r.get("b1") is not None


# ── HealthMonitor integration ───────────────────────────────────────────────


def test_monitor_check_calls_sweep_each_tick():
    """The HealthMonitor sweep is what actually keeps `_workers` from
    growing. Stub `_check_all`'s expensive paths and verify each tick
    invokes sweep_stale with the configured grace."""
    redis = MagicMock()
    redis.ttl = AsyncMock(return_value=10)        # heartbeat alive
    redis.exists = AsyncMock(return_value=1)       # completion key present

    r = WorkerRegistry()
    # Add an OLD done entry that the sweep should remove.
    r._workers["old"] = _entry("old", status="done",
                                done_at=time.time() - 200)

    monitor = HealthMonitor(
        registry=r, spawner=MagicMock(), redis=redis,
        heartbeat_key_tpl="worker:heartbeat:{bug_id}",
        completed_key_tpl="worker:completed:{bug_id}",
        done_grace_seconds=30,
    )
    asyncio.run(monitor._check_all())
    assert r.get("old") is None
