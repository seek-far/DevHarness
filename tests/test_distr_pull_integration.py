"""
Integration test for the distr-pull mode.

Requires a running Redis instance.  All tests in this file are skipped
when Redis is not reachable on ``REDIS_URL`` (default localhost:6379).

Use a dedicated Redis DB (14) to avoid interfering with other tests.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import redis.asyncio as aioredis

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def _redis_host_port():
    url = __import__("os").environ.get("REDIS_URL", "redis://localhost:6379")
    try:
        host = url.split("://")[1].split(":")[0]
        port = int(url.split(":")[-1].split("/")[0])
        return host, port
    except (IndexError, ValueError):
        return "localhost", 6379


def _redis_reachable():
    import socket as _s
    host, port = _redis_host_port()
    try:
        s = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        s.settimeout(1)
        s.connect((host, port))
        s.close()
        return True
    except (OSError, _s.error):
        return False


# All tests are async.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not _redis_reachable(),
        reason="Redis not reachable — start redis-server or set REDIS_URL",
    ),
]

TEST_REDIS_DB = 14


def _redis_url(db=TEST_REDIS_DB):
    base = os.environ.get("REDIS_URL", "redis://localhost:6379")
    if "/" in base.rsplit(":", 1)[-1]:
        parts = base.rsplit("/", 1)
        return f"{parts[0]}/{db}"
    return f"{base}/{db}"


async def _cleanup(r):
    keys_to_delete = []
    async for key in r.scan_iter(match="worker-daemon*"):
        keys_to_delete.append(key)
    async for key in r.scan_iter(match="task:owner:*"):
        keys_to_delete.append(key)
    if keys_to_delete:
        await r.delete(*keys_to_delete)


# ── fixture ─────────────────────────────────────────────────────

@pytest.fixture
async def redis():
    r = aioredis.from_url(_redis_url(), decode_responses=False)
    await _cleanup(r)
    yield r
    await _cleanup(r)
    await r.aclose()


# ── daemon registration and heartbeat ───────────────────────────

class TestDaemonRegistration:
    async def test_register_adds_to_daemon_set(self, redis):
        await redis.sadd("worker-daemons", "test-reg")
        members = await redis.smembers("worker-daemons")
        members_str = {m.decode() if isinstance(m, bytes) else m for m in members}
        assert "test-reg" in members_str

    async def test_heartbeat_writes_hash_fields(self, redis):
        await redis.hset("worker-daemon:test-hb", mapping={
            "free_slots": "4", "free_memory_mb": "8192",
            "hostname": "test-box", "concurrency": "4", "last_hb_ts": "999999",
        })
        data = await redis.hgetall("worker-daemon:test-hb")
        assert data[b"free_slots"] == b"4"
        assert data[b"hostname"] == b"test-box"

    async def test_heartbeat_key_has_ttl(self, redis):
        await redis.hset("worker-daemon:test-ttl", mapping={"free_slots": "1"})
        await redis.expire("worker-daemon:test-ttl", 30)
        ttl = await redis.ttl("worker-daemon:test-ttl")
        assert ttl > 0


# ── dispatch round-robin ─────────────────────────────────────────

class TestDispatchRoundRobin:
    async def test_dispatches_to_free_daemon(self, redis):
        from orchestrator.dispatcher import DistributedDispatcher
        from orchestrator.registry import WorkerRegistry

        await redis.sadd("worker-daemons", "A")
        await redis.hset("worker-daemon:A", mapping={
            "free_slots": "3", "free_memory_mb": "8192",
            "hostname": "n1", "concurrency": "4", "last_hb_ts": str(int(1e15)),
        })
        disp = DistributedDispatcher(redis, WorkerRegistry(), cache_ttl=0)
        await disp.spawn("bug-d1", "42", "url", "99", "main")

        msgs = await redis.xrange("worker-daemon:A:inbox", "-", "+")
        assert len(msgs) >= 1

    async def test_round_robin_skips_full_daemon(self, redis):
        from orchestrator.dispatcher import DistributedDispatcher
        from orchestrator.registry import WorkerRegistry

        await redis.sadd("worker-daemons", "A", "B")
        await redis.hset("worker-daemon:A", mapping={
            "free_slots": "0", "free_memory_mb": "8192",
            "hostname": "n1", "concurrency": "4", "last_hb_ts": str(int(1e15)),
        })
        await redis.hset("worker-daemon:B", mapping={
            "free_slots": "2", "free_memory_mb": "8192",
            "hostname": "n2", "concurrency": "4", "last_hb_ts": str(int(1e15)),
        })
        disp = DistributedDispatcher(redis, WorkerRegistry(), cache_ttl=0)
        await disp.spawn("bug-d2", "42", "url", "99", "")

        a_msgs = await redis.xrange("worker-daemon:A:inbox", "-", "+")
        assert len(a_msgs) == 0
        b_msgs = await redis.xrange("worker-daemon:B:inbox", "-", "+")
        assert len(b_msgs) >= 1

    async def test_all_full_enqueues_pending(self, redis):
        from orchestrator.dispatcher import DistributedDispatcher
        from orchestrator.registry import WorkerRegistry

        await redis.sadd("worker-daemons", "A")
        await redis.hset("worker-daemon:A", mapping={
            "free_slots": "0", "free_memory_mb": "8192",
            "hostname": "n1", "concurrency": "4", "last_hb_ts": str(int(1e15)),
        })
        disp = DistributedDispatcher(redis, WorkerRegistry(), cache_ttl=0)
        await disp.spawn("bug-pend", "1", "u", "j", "")

        msgs = await redis.xrange("worker-daemon:pending", "-", "+")
        assert len(msgs) >= 1


# ── daemon monitor — pending retry ───────────────────────────────

class TestDaemonMonitorPending:
    async def test_retries_pending_when_daemon_frees_up(self, redis):
        from orchestrator.dispatcher import DistributedDispatcher
        from orchestrator.daemon_monitor import DaemonMonitor
        from orchestrator.registry import WorkerRegistry

        await redis.sadd("worker-daemons", "A")
        await redis.hset("worker-daemon:A", mapping={
            "free_slots": "0", "free_memory_mb": "8192",
            "hostname": "n1", "concurrency": "4", "last_hb_ts": str(int(1e15)),
        })
        disp = DistributedDispatcher(redis, WorkerRegistry(), cache_ttl=0)
        await disp.spawn("bug-retry", "42", "url", "99", "")

        await redis.hset("worker-daemon:A", "free_slots", "2")

        try:
            await redis.xgroup_create("worker-daemon:pending", "orch-group",
                                      mkstream=True)
        except Exception:
            pass

        monitor = DaemonMonitor(redis, disp, interval=999)
        await monitor._retry_pending()

        msgs = await redis.xrange("worker-daemon:A:inbox", "-", "+")
        assert len(msgs) >= 1


# ── message router fallback ──────────────────────────────────────

class TestRouterFallback:
    async def test_routes_via_task_owner_key(self):
        from orchestrator.router import MessageRouter
        from orchestrator.registry import WorkerRegistry

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=b"daemon-X")
        mock_redis.xadd = AsyncMock()

        reg = WorkerRegistry()
        router = MessageRouter(reg, mock_redis, "worker:{bug_id}:stream")

        from types import SimpleNamespace
        event = SimpleNamespace(bug_id="bug-route", status="success",
                                raw={"status": "success"})

        result = await router.route(event)
        assert result is True
        mock_redis.xadd.assert_called_once()


# ── full lifecycle ──────────────────────────────────────────────

class TestFullLifecycle:
    async def test_end_to_end_dispatch_and_stream(self, redis):
        from orchestrator.dispatcher import DistributedDispatcher
        from orchestrator.registry import WorkerRegistry

        daemon_id = "e2e"
        inbox_key = f"worker-daemon:{daemon_id}:inbox"

        await redis.sadd("worker-daemons", daemon_id)
        await redis.hset(f"worker-daemon:{daemon_id}", mapping={
            "free_slots": "3", "free_memory_mb": "8192",
            "hostname": "e2e-box", "concurrency": "4",
            "last_hb_ts": str(int(1e15)),
        })

        disp = DistributedDispatcher(redis, WorkerRegistry(), cache_ttl=0)
        await disp.spawn("bug-e2e", "42", "https://g/org/r", "99", "main")

        msgs = await redis.xrange(inbox_key, "-", "+")
        assert len(msgs) >= 1
        _, fields = msgs[-1]
        assert fields[b"bug_id"] == b"bug-e2e"

        try:
            await redis.xgroup_create(inbox_key, "daemon-group", mkstream=True)
        except Exception:
            pass

        read = await redis.xreadgroup(
            groupname="daemon-group", consumername=daemon_id,
            streams={inbox_key: ">"}, count=1, block=1000,
        )
        assert read is not None

        await redis.set("task:owner:bug-e2e", daemon_id, ex=3600)
        if read:
            for _, entries in read:
                for mid, _ in entries:
                    await redis.xack(inbox_key, "daemon-group", mid)

        pending = await redis.xpending(inbox_key, "daemon-group")
        if isinstance(pending, dict):
            assert pending.get("pending", 0) == 0

        await redis.delete("task:owner:bug-e2e")
        owner = await redis.get("task:owner:bug-e2e")
        assert owner is None
