"""StreamConsumer startup resilience — the self-healing supervisor.

Incident (2026-06-03, K8s gitlab@minus, full-host reboot): redis and the
orchestrator pod started together. At 08:02 the orchestrator's consume task
ran `_ensure_group()` while redis was still coming up; `xgroup_create` raised
a non-BUSYGROUP error (ConnectionError / "LOADING"). `_ensure_group` only
catches BUSYGROUP, so it re-raised — and because that call sat in `_run()`
BEFORE the read loop's own try/except, it escaped `_run()` and killed the
consume task. `run()` never awaits this task (it blocks on a shutdown signal),
so the exception was stored on the Task and never retrieved: no traceback, pod
stayed 1/1, and the consumer never read again. Redis was healthy seconds
later, but nothing restarted the dead task. Symptom: gateway XADDs piled up
(`lag` grew) while the consumer's `idle` climbed for ~12h with zero recovery.

The fix has two layers, pinned here:
  * `_run()` wraps ensure_group + drain + read-loop in ONE retry, so a
    transient redis error at startup self-heals once redis comes up instead
    of killing the task.
  * `start()` attaches a done-callback that logs CRITICAL on any unexpected
    task exit — so a future silent death can never again be invisible.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.consumer import StreamConsumer


def _consumer(redis, handler, *, count: int = 10) -> StreamConsumer:
    return StreamConsumer(
        redis=redis,
        stream_key="gateway:stream",
        group="orchestrator-group",
        consumer_name="consumer-1",
        handler=handler,
        dead_letter_stream="orchestrator:dead_letter",
        block_ms=1000,
        count=count,
    )


def _xreadgroup_result(entries):
    return [(b"gateway:stream", entries)] if entries else []


@pytest.fixture
def _fast_sleep(monkeypatch):
    """Make every asyncio.sleep return on the next loop tick (still yields,
    so other tasks run) — keeps the retry-driven tests sub-millisecond."""
    real_sleep = asyncio.sleep

    async def fast_sleep(_delay, *a, **k):
        await real_sleep(0)

    # consumer.py references the shared asyncio module object, so this patches
    # both the consumer's sleeps and (harmlessly) our own real_sleep(0) yields.
    monkeypatch.setattr("orchestrator.consumer.asyncio.sleep", fast_sleep)
    return real_sleep


def test_run_self_heals_transient_ensure_group_error(_fast_sleep):
    """The headline regression: first `_ensure_group` attempt fails with a
    transient (non-BUSYGROUP) redis error — the task must NOT die. `_run`
    retries, the group ensures on the second pass, and a queued message is
    then consumed."""
    real_sleep = _fast_sleep
    handled: list[bytes] = []

    async def handler(raw: bytes):
        handled.append(raw)

    redis = MagicMock()
    # 1st: redis not ready yet → transient error. 2nd: group already exists.
    redis.xgroup_create = AsyncMock(side_effect=[
        ConnectionError("Error 111 connecting to redis:6379. Connection refused."),
        Exception("BUSYGROUP Consumer Group name already exists"),
    ])
    redis.xack = AsyncMock()
    redis.xadd = AsyncMock()

    # First xreadgroup is the PEL drain (empty → returns). Then the main ">"
    # loop delivers one entry; afterwards it sees empty and spins until we
    # cancel.
    reads = [
        _xreadgroup_result([]),                                   # drain pass
        _xreadgroup_result([(b"1-0", {b"data": b'{"x": 1}'})]),  # main read
    ]

    async def fake_xreadgroup(**kwargs):
        await real_sleep(0)  # guarantee a yield each iteration
        return reads.pop(0) if reads else _xreadgroup_result([])

    redis.xreadgroup = fake_xreadgroup

    async def drive():
        task = asyncio.get_event_loop().create_task(c._run())
        for _ in range(200):
            await real_sleep(0)
            if handled:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    c = _consumer(redis, handler)
    asyncio.run(drive())

    assert handled == [b'{"x": 1}']
    # Retried ensure_group after the transient failure (didn't die on it).
    assert redis.xgroup_create.await_count == 2


def test_run_keeps_retrying_until_redis_ready(_fast_sleep):
    """Generalises the above: redis stays down for several startup attempts,
    then recovers. The task must keep retrying (not give up, not die) and
    consume once the group can finally be ensured."""
    real_sleep = _fast_sleep
    handled: list[bytes] = []

    async def handler(raw: bytes):
        handled.append(raw)

    redis = MagicMock()
    redis.xgroup_create = AsyncMock(side_effect=[
        ConnectionError("down"),
        ConnectionError("down"),
        ConnectionError("down"),
        Exception("BUSYGROUP Consumer Group name already exists"),
    ])
    redis.xack = AsyncMock()
    reads = [
        _xreadgroup_result([]),
        _xreadgroup_result([(b"9-0", {b"data": b"payload"})]),
    ]

    async def fake_xreadgroup(**kwargs):
        await real_sleep(0)
        return reads.pop(0) if reads else _xreadgroup_result([])

    redis.xreadgroup = fake_xreadgroup

    async def drive():
        task = asyncio.get_event_loop().create_task(c._run())
        for _ in range(400):
            await real_sleep(0)
            if handled:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    c = _consumer(redis, handler)
    asyncio.run(drive())

    assert handled == [b"payload"]
    assert redis.xgroup_create.await_count == 4


def test_done_callback_logs_critical_on_unexpected_exit(caplog):
    """A task that exits with an exception must surface a CRITICAL log via
    the done-callback — no more silent death."""
    caplog.set_level(logging.CRITICAL, logger="orchestrator.consumer")

    async def boom():
        raise RuntimeError("kaboom")

    async def drive():
        t = asyncio.get_event_loop().create_task(boom())
        with pytest.raises(RuntimeError):
            await t
        StreamConsumer._on_task_done(t)

    asyncio.run(drive())
    assert any("EXITED unexpectedly" in r.message for r in caplog.records)


def test_done_callback_silent_on_clean_cancel(caplog):
    """A normal shutdown cancels the task — the callback must stay quiet so
    operators aren't paged on every graceful stop."""
    caplog.set_level(logging.DEBUG, logger="orchestrator.consumer")

    async def forever():
        await asyncio.sleep(100)

    async def drive():
        t = asyncio.get_event_loop().create_task(forever())
        await asyncio.sleep(0)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        StreamConsumer._on_task_done(t)

    asyncio.run(drive())
    assert not any("EXITED" in r.message for r in caplog.records)
