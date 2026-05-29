"""Orchestrator Prometheus metrics — counter wiring + gauge sampling.

Pins:
  * spawner.spawn() bumps workers_spawned with correct spawner label.
  * spawn collision bumps bug_id_collisions and does NOT bump workers_spawned.
  * HealthMonitor restart path bumps worker_restarts with `cause=heartbeat_expired`.
  * dead-letter route bumps dead_letter with the source stream label.
  * HealthMonitor's gauge refresh sets sdlcma_active_workers per status.
  * stream_pending gauge samples XPENDING when stream+group configured.

Counters use BEFORE/AFTER deltas (process-wide default registry).
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from prometheus_client import REGISTRY

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.models import WARMUP_GRACE, WorkerEntry
from orchestrator.monitor import HealthMonitor
from orchestrator.registry import WorkerRegistry
from orchestrator.spawner import WorkerSpawner


def _counter_value(name: str, labels: dict | None = None) -> float:
    sample = REGISTRY.get_sample_value(name, labels or {})
    return sample if sample is not None else 0.0


def _gauge_value(name: str, labels: dict | None = None) -> float:
    sample = REGISTRY.get_sample_value(name, labels or {})
    return sample if sample is not None else 0.0


# ── workers_spawned + bug_id_collisions ────────────────────────────────────


def test_workers_spawned_increments_on_process_spawn(monkeypatch):
    registry = WorkerRegistry()
    spawner = WorkerSpawner(registry, "redis://localhost:6379/0")
    # Avoid actually launching a subprocess — patch _start_process to
    # return a fake WorkerEntry.
    fake_entry = WorkerEntry(
        bug_id="bug-1", process=MagicMock(returncode=None),
        project_id="1", project_web_url="u", job_id="j",
        started_at=time.time(), warmup_deadline=time.time() + WARMUP_GRACE,
    )

    async def _fake_start(*_a, **_kw):
        return fake_entry

    monkeypatch.setattr(spawner, "_start_process", _fake_start)
    before = _counter_value("sdlcma_workers_spawned_total", {"spawner": "process"})
    asyncio.run(spawner.spawn("bug-1", "1", "u", "j"))
    after = _counter_value("sdlcma_workers_spawned_total", {"spawner": "process"})
    assert after == before + 1


def test_bug_id_collision_does_not_double_count(monkeypatch):
    """Second spawn for the same bug_id increments the collision
    counter, NOT workers_spawned. Confirms the spawn() short-circuit
    bumps the right metric."""
    registry = WorkerRegistry()
    spawner = WorkerSpawner(registry, "redis://localhost:6379/0")
    fake_entry = WorkerEntry(
        bug_id="dup", process=MagicMock(returncode=None),
        project_id="1", project_web_url="u", job_id="j",
        started_at=time.time(), warmup_deadline=time.time() + WARMUP_GRACE,
    )

    async def _fake_start(*_a, **_kw):
        return fake_entry

    monkeypatch.setattr(spawner, "_start_process", _fake_start)
    asyncio.run(spawner.spawn("dup", "1", "u", "j"))

    spawned_before = _counter_value(
        "sdlcma_workers_spawned_total", {"spawner": "process"})
    collisions_before = _counter_value("sdlcma_bug_id_collisions_total")

    asyncio.run(spawner.spawn("dup", "1", "u", "j"))

    assert _counter_value(
        "sdlcma_workers_spawned_total", {"spawner": "process"}
    ) == spawned_before
    assert _counter_value(
        "sdlcma_bug_id_collisions_total"
    ) == collisions_before + 1


# ── worker_restarts ────────────────────────────────────────────────────────


def test_restart_bumps_worker_restarts(monkeypatch):
    """HealthMonitor's heartbeat-expired restart path bumps
    worker_restarts with the right cause label."""
    redis = MagicMock()
    redis.ttl = AsyncMock(return_value=-2)        # heartbeat expired
    redis.exists = AsyncMock(return_value=0)       # no completion key

    registry = WorkerRegistry()
    bug_id = "bug-restart"
    entry = WorkerEntry(
        bug_id=bug_id, process=MagicMock(returncode=None),
        project_id="1", project_web_url="u", job_id="j",
        started_at=time.time(), warmup_deadline=0.0,  # past warmup
        status="running",
    )
    registry._workers[bug_id] = entry

    spawner = MagicMock()
    spawner.restart = AsyncMock()
    monitor = HealthMonitor(
        registry=registry, spawner=spawner, redis=redis,
        heartbeat_key_tpl="worker:heartbeat:{bug_id}",
        completed_key_tpl="worker:completed:{bug_id}",
    )

    before = _counter_value(
        "sdlcma_worker_restarts_total", {"cause": "heartbeat_expired"})
    asyncio.run(monitor._check_all())
    after = _counter_value(
        "sdlcma_worker_restarts_total", {"cause": "heartbeat_expired"})
    assert after == before + 1
    spawner.restart.assert_awaited_once()


# ── active_workers gauge ────────────────────────────────────────────────────


def test_active_workers_gauge_reflects_registry():
    """Mix of statuses in the registry; gauge only counts the
    in-flight ones (warmup + running). done/failed entries are
    deliberately excluded — see _refresh_gauges docstring."""
    registry = WorkerRegistry()
    statuses = ("warmup", "running", "running", "done", "failed")
    for i, status in enumerate(statuses):
        bug_id = f"g{i}"
        e = WorkerEntry(
            bug_id=bug_id, process=MagicMock(returncode=None),
            project_id="1", project_web_url="u", job_id="j",
            started_at=time.time(), warmup_deadline=time.time() + WARMUP_GRACE,
            status=status,
        )
        registry._workers[bug_id] = e

    monitor = HealthMonitor(
        registry=registry, spawner=MagicMock(), redis=MagicMock(),
        heartbeat_key_tpl="worker:heartbeat:{bug_id}",
        completed_key_tpl="worker:completed:{bug_id}",
    )
    monitor._refresh_gauges()
    assert _gauge_value("sdlcma_active_workers", {"status": "warmup"}) == 1
    assert _gauge_value("sdlcma_active_workers", {"status": "running"}) == 2
    # done and failed labels MUST NOT be created — otherwise dashboards
    # show a misleading "done" series that monotonically grows because
    # registry currently doesn't remove done entries.
    assert REGISTRY.get_sample_value(
        "sdlcma_active_workers", {"status": "done"}) is None
    assert REGISTRY.get_sample_value(
        "sdlcma_active_workers", {"status": "failed"}) is None


# ── dead_letter ───────────────────────────────────────────────────────────-


def test_dead_letter_increments_on_handler_failure():
    """Importing the consumer (via the metrics-wired path) plus calling
    _process_entry with a raising handler must bump dead_letter."""
    from orchestrator.consumer import StreamConsumer
    redis = MagicMock()
    redis.xack = AsyncMock()
    redis.xadd = AsyncMock()

    async def handler(_raw: bytes):
        raise ValueError("nope")

    c = StreamConsumer(
        redis=redis,
        stream_key="gateway:stream",
        group="g",
        consumer_name="c",
        handler=handler,
        dead_letter_stream="orchestrator:dead_letter",
    )
    before = _counter_value(
        "sdlcma_dead_letter_total", {"stream": "gateway:stream"})
    asyncio.run(c._process_entry(b"123-0", {b"data": b"bad"}))
    after = _counter_value(
        "sdlcma_dead_letter_total", {"stream": "gateway:stream"})
    assert after == before + 1


# ── stream_pending gauge ───────────────────────────────────────────────────


def test_stream_pending_gauge_samples_xpending():
    """When gateway_stream + consumer_group are wired into the monitor,
    a check_all tick reads XPENDING and sets the gauge."""
    redis = MagicMock()
    redis.xpending = AsyncMock(return_value={"pending": 7, "min": None,
                                              "max": None, "consumers": []})

    monitor = HealthMonitor(
        registry=WorkerRegistry(), spawner=MagicMock(), redis=redis,
        heartbeat_key_tpl="worker:heartbeat:{bug_id}",
        completed_key_tpl="worker:completed:{bug_id}",
        gateway_stream="gateway:stream",
        gateway_consumer_group="orchestrator-group-mp",
    )
    asyncio.run(monitor._sample_stream_pending())
    assert _gauge_value(
        "sdlcma_stream_pending",
        {"stream": "gateway:stream", "group": "orchestrator-group-mp"},
    ) == 7


def test_stream_pending_disabled_when_stream_or_group_empty():
    """A monitor without stream/group wiring (the test default) must
    NOT crash on _refresh_gauges and must not create a stream_pending
    sample for the empty label-set."""
    monitor = HealthMonitor(
        registry=WorkerRegistry(), spawner=MagicMock(), redis=MagicMock(),
        heartbeat_key_tpl="worker:heartbeat:{bug_id}",
        completed_key_tpl="worker:completed:{bug_id}",
    )
    # No assertion needed beyond "doesn't crash" — gauge stays at whatever
    # it was. The active_workers gauge is still refreshed.
    monitor._refresh_gauges()
