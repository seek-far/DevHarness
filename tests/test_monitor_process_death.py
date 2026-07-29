"""HealthMonitor must tell a finished worker apart from a killed one.

Until 2026-07-30 the Monitor treated ANY observed process exit as "done",
without looking at the exit code. A worker killed by the OOM killer, or by an
operator, was therefore marked done and silently abandoned — no restart, no
error, the bug simply never got fixed. The multi-node plan expects exactly that
failure (several ver99 workers landing on one node until it runs out of
memory), and it surfaced concretely while testing W2's intra-loop resume: a
deliberately SIGKILLed worker was never re-spawned, so nothing ever exercised
the resume path in production shape.

The other half is the cap. Both restart triggers — heartbeat expiry and
abnormal exit — re-arm on the replacement worker, so without a bound a worker
that dies deterministically loops forever, re-spending its LLM budget each
time. The heartbeat path had no cap even before this change.

See /mnt/d/PL/sdlcma/W2-step-checkpoint-design.md §18.6 and
/mnt/d/PL/sdlcma/k8s-ha-multinode-plan.md W5.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.models import MAX_WORKER_RESTARTS  # noqa: E402
from orchestrator.monitor import HealthMonitor  # noqa: E402
from orchestrator.registry import WorkerRegistry  # noqa: E402


def _registry(bug_id="bug-1", *, returncode=None, restart_count=0):
    reg = WorkerRegistry()
    proc = MagicMock()
    proc.returncode = returncode
    proc.reload_status = MagicMock()
    entry = SimpleNamespace(
        bug_id=bug_id, process=proc, project_id="42",
        project_web_url="u", job_id="j", status="running",
        warmup_deadline=0, restart_count=restart_count, source_branch="instance/x",
    )
    reg._workers[bug_id] = entry
    return reg, entry


def _monitor(redis, registry, spawner):
    return HealthMonitor(
        registry=registry, spawner=spawner, redis=redis,
        heartbeat_key_tpl="worker:heartbeat:{bug_id}",
        completed_key_tpl="worker:completed:{bug_id}",
        check_interval=20,
    )


def _redis(*, completed: bool):
    r = MagicMock()
    r.ttl = AsyncMock(return_value=-2)
    r.exists = AsyncMock(return_value=1 if completed else 0)
    return r


def _spawner():
    s = MagicMock()
    s.restart = AsyncMock()
    return s


# ── the fix ──────────────────────────────────────────────────────────────────

def test_sigkilled_worker_is_restarted():
    """rc=-9 with no completion key is a death, not a completion."""
    reg, entry = _registry(returncode=-9)
    spawner = _spawner()
    asyncio.run(_monitor(_redis(completed=False), reg, spawner)._check_all())

    spawner.restart.assert_called_once()
    assert spawner.restart.call_args.args[0] == "bug-1"
    # Restarting against the same base branch is what lets the replacement
    # re-attach to the container the dead worker left running.
    assert spawner.restart.call_args.kwargs["source_branch"] == "instance/x"


def test_crashed_worker_is_restarted():
    """A non-zero exit before completion is the same class of event."""
    reg, _ = _registry(returncode=1)
    spawner = _spawner()
    asyncio.run(_monitor(_redis(completed=False), reg, spawner)._check_all())
    spawner.restart.assert_called_once()


# ── what must NOT change ─────────────────────────────────────────────────────

def test_clean_exit_is_still_done():
    reg, _ = _registry(returncode=0)
    spawner = _spawner()
    asyncio.run(_monitor(_redis(completed=False), reg, spawner)._check_all())

    spawner.restart.assert_not_called()
    assert reg.get("bug-1").status == "done"


def test_nonzero_exit_with_completion_key_is_done():
    """The worker SETs that key in its `finally` on every exit path.

    Its presence means the run really finished, whatever the exit code says —
    the same tie-breaker the heartbeat path already used to stop ECS's slow
    STOPPED reporting from causing spurious restarts.
    """
    reg, _ = _registry(returncode=1)
    spawner = _spawner()
    asyncio.run(_monitor(_redis(completed=True), reg, spawner)._check_all())

    spawner.restart.assert_not_called()
    assert reg.get("bug-1").status == "done"


# ── the cap ──────────────────────────────────────────────────────────────────

def test_restarts_are_capped():
    """A deterministically dying worker must stop being re-spawned."""
    reg, _ = _registry(returncode=-9, restart_count=MAX_WORKER_RESTARTS)
    spawner = _spawner()
    asyncio.run(_monitor(_redis(completed=False), reg, spawner)._check_all())

    spawner.restart.assert_not_called()
    assert reg.get("bug-1").status == "failed"


def test_cap_applies_to_the_heartbeat_path_too():
    """One rule for both triggers — the heartbeat path was unbounded before."""
    reg, _ = _registry(returncode=None, restart_count=MAX_WORKER_RESTARTS)
    spawner = _spawner()
    asyncio.run(_monitor(_redis(completed=False), reg, spawner)._check_all())

    spawner.restart.assert_not_called()
    assert reg.get("bug-1").status == "failed"


def test_below_the_cap_still_restarts():
    reg, _ = _registry(returncode=-9, restart_count=MAX_WORKER_RESTARTS - 1)
    spawner = _spawner()
    asyncio.run(_monitor(_redis(completed=False), reg, spawner)._check_all())
    spawner.restart.assert_called_once()
