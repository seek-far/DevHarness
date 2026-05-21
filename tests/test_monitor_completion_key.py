"""HealthMonitor must not restart workers that have already completed.

The original heartbeat-expired-then-restart rule races with ECS, which can
take 30-60s to report `lastStatus=STOPPED` back to `describe_tasks` after the
container process exits. In that window, `entry.process.returncode` is still
None (the spawner-side proxy hasn't observed STOPPED), and the heartbeat key
has already expired (its TTL is 30s). Without a tie-breaker the Monitor
restarts a worker that successfully finished, often dozens of times — verified
on the 2026-05-21 AWS ECS deploy where MR !6 opened cleanly but the registry
entry was then restarted 37 times before ECS ran out of placement capacity.

The fix: the worker SETs `worker:completed:{bug_id}` in its `finally` block on
every exit path (fixed / no_fix / error / R10 short-circuit). The Monitor
checks this key BEFORE restarting on heartbeat expiry and marks done if it's
present — i.e. "heartbeat is gone because the worker is gone *cleanly*".
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.monitor import HealthMonitor  # noqa: E402
from orchestrator.registry import WorkerRegistry  # noqa: E402


def _registry_with_running_entry(bug_id="bug-1"):
    reg = WorkerRegistry()
    # Fake "process" interface (HealthMonitor uses .returncode + reload_status).
    proc = MagicMock()
    proc.returncode = None  # still alive from process-status perspective
    proc.reload_status = MagicMock()
    entry = SimpleNamespace(
        bug_id=bug_id, process=proc, project_id="42",
        project_web_url="u", job_id="j", status="running",
        warmup_deadline=0, restart_count=0,
    )
    reg._workers[bug_id] = entry  # bypass register() for test setup
    return reg, entry


def _make_monitor(redis_mock, registry, spawner_mock=None):
    return HealthMonitor(
        registry=registry,
        spawner=spawner_mock or MagicMock(),
        redis=redis_mock,
        heartbeat_key_tpl="worker:heartbeat:{bug_id}",
        completed_key_tpl="worker:completed:{bug_id}",
        check_interval=20,
    )


def test_completion_key_present_marks_done_not_restart():
    redis = MagicMock()
    redis.ttl = AsyncMock(return_value=-2)       # heartbeat expired/missing
    redis.exists = AsyncMock(return_value=1)     # completion key SET

    reg, entry = _registry_with_running_entry("bug-1")
    spawner = MagicMock()
    spawner.restart = AsyncMock()
    monitor = _make_monitor(redis, reg, spawner_mock=spawner)

    asyncio.run(monitor._check_all())

    spawner.restart.assert_not_called()  # this is the load-bearing assertion
    assert reg.get("bug-1").status == "done"


def test_completion_key_absent_falls_back_to_restart():
    """The fix must not suppress real crash recovery: heartbeat expired AND
    no completion key still means restart."""
    redis = MagicMock()
    redis.ttl = AsyncMock(return_value=-2)
    redis.exists = AsyncMock(return_value=0)     # no completion key

    reg, entry = _registry_with_running_entry("bug-x")
    spawner = MagicMock()
    spawner.restart = AsyncMock()
    monitor = _make_monitor(redis, reg, spawner_mock=spawner)

    asyncio.run(monitor._check_all())

    spawner.restart.assert_called_once()


def test_returncode_already_set_short_circuits_before_completion_check():
    """If the spawner-side proxy already saw STOPPED + exitCode, the original
    rc-based done path fires first — completion key is just a fallback."""
    redis = MagicMock()
    redis.ttl = AsyncMock(return_value=-2)
    redis.exists = AsyncMock(return_value=0)     # absent — should be irrelevant

    reg, entry = _registry_with_running_entry("bug-rc")
    entry.process.returncode = 0  # process visibly done
    spawner = MagicMock()
    spawner.restart = AsyncMock()
    monitor = _make_monitor(redis, reg, spawner_mock=spawner)

    asyncio.run(monitor._check_all())

    spawner.restart.assert_not_called()
    assert reg.get("bug-rc").status == "done"
    # Completion-key path was never consulted in this branch.
    redis.exists.assert_not_called()
