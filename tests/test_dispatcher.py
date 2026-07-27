"""
Unit tests for DistributedDispatcher — round-robin daemon selection.

Tests the dispatch algorithm in isolation using a fake Redis.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ── helpers ────────────────────────────────────────────────────────

def _make_daemon_hash(free_slots=4, free_memory_mb=8192, hostname="test",
                      concurrency=4, last_hb_ts=999999999999):
    """Build a fake Redis Hash response (bytes→bytes dict) for a daemon."""
    return {
        b"free_slots": str(free_slots).encode(),
        b"free_memory_mb": str(free_memory_mb).encode(),
        b"hostname": hostname.encode(),
        b"concurrency": str(concurrency).encode(),
        b"last_hb_ts": str(last_hb_ts).encode(),
    }


def _fake_redis(*, smembers_result=None, hgetall_map=None):
    """Build a fake aioredis mock with smembers / hgetall / xadd / xgroup_create."""
    redis = AsyncMock()
    if smembers_result is not None:
        redis.smembers = AsyncMock(return_value=smembers_result)
    if hgetall_map is not None:
        async def _hgetall(key):
            return hgetall_map.get(key, {})
        redis.hgetall = AsyncMock(side_effect=_hgetall)
    redis.xadd = AsyncMock()
    redis.xgroup_create = AsyncMock()
    return redis


# ── DaemonInfo ─────────────────────────────────────────────────────

class TestDaemonInfo:
    def test_from_hash_parses_all_fields(self):
        from orchestrator.dispatcher import DaemonInfo
        raw = _make_daemon_hash(free_slots=3, free_memory_mb=4096,
                                hostname="node1", concurrency=8,
                                last_hb_ts=1700000000000)
        info = DaemonInfo.from_hash("A", raw)
        assert info.daemon_id == "A"
        assert info.free_slots == 3
        assert info.free_memory_mb == 4096
        assert info.hostname == "node1"
        assert info.concurrency == 8
        assert info.last_hb_ts == 1700000000000

    def test_is_alive_true_when_has_heartbeat(self):
        from orchestrator.dispatcher import DaemonInfo
        info = DaemonInfo("A", last_hb_ts=1700000000000)
        assert info.is_alive

    def test_is_alive_false_when_no_heartbeat(self):
        from orchestrator.dispatcher import DaemonInfo
        info = DaemonInfo("A", last_hb_ts=0)
        assert not info.is_alive

    def test_can_accept_free_slots_and_memory_ok(self):
        from orchestrator.dispatcher import DaemonInfo
        info = DaemonInfo("A", free_slots=2, free_memory_mb=4096)
        assert info.can_accept(min_free_memory_mb=2048)

    def test_can_accept_rejects_zero_slots(self):
        from orchestrator.dispatcher import DaemonInfo
        info = DaemonInfo("A", free_slots=0, free_memory_mb=4096)
        assert not info.can_accept(min_free_memory_mb=2048)

    def test_can_accept_rejects_low_memory(self):
        from orchestrator.dispatcher import DaemonInfo
        info = DaemonInfo("A", free_slots=2, free_memory_mb=1024)
        assert not info.can_accept(min_free_memory_mb=2048)


# ── DistributedDispatcher — daemon selection ───────────────────────

class TestChooseDaemon:
    """Tests for DistributedDispatcher._choose_daemon()"""

    def _make_dispatcher(self, redis, cache_ttl=0):
        from orchestrator.dispatcher import DistributedDispatcher
        from orchestrator.registry import WorkerRegistry
        reg = WorkerRegistry()
        return DistributedDispatcher(redis, reg, cache_ttl=cache_ttl)

    def test_round_robin_picks_first_free(self):
        """A is full, B is free → B selected. Pointer advances past B."""
        redis = _fake_redis(
            smembers_result={b"A", b"B"},
            hgetall_map={
                "worker-daemon:A": _make_daemon_hash(free_slots=0),
                "worker-daemon:B": _make_daemon_hash(free_slots=3),
            },
        )
        disp = self._make_dispatcher(redis)
        disp._judge_pointer = 0
        disp._cache_fetched_at = 0

        result = asyncio.run(disp._choose_daemon())
        assert result == "B"
        assert disp._judge_pointer == 0

    def test_respects_judge_pointer_start(self):
        """Starts from pointer position, not always from index 0."""
        redis = _fake_redis(
            smembers_result={b"A", b"B", b"C"},
            hgetall_map={
                "worker-daemon:A": _make_daemon_hash(free_slots=2),
                "worker-daemon:B": _make_daemon_hash(free_slots=2),
                "worker-daemon:C": _make_daemon_hash(free_slots=2),
            },
        )
        disp = self._make_dispatcher(redis)
        disp._judge_pointer = 2

        result = asyncio.run(disp._choose_daemon())
        assert result == "C"
        assert disp._judge_pointer == 0

    def test_all_full_returns_none(self):
        """When all daemons are full, return None."""
        redis = _fake_redis(
            smembers_result={b"A", b"B"},
            hgetall_map={
                "worker-daemon:A": _make_daemon_hash(free_slots=0),
                "worker-daemon:B": _make_daemon_hash(free_slots=0),
            },
        )
        disp = self._make_dispatcher(redis)
        disp._judge_pointer = 0

        result = asyncio.run(disp._choose_daemon())
        assert result is None
        assert disp._judge_pointer == 1

    def test_skips_dead_daemon(self):
        """Daemon with no heartbeat (last_hb_ts=0) is skipped."""
        redis = _fake_redis(
            smembers_result={b"A", b"B"},
            hgetall_map={
                "worker-daemon:A": _make_daemon_hash(last_hb_ts=0),
                "worker-daemon:B": _make_daemon_hash(free_slots=2),
            },
        )
        disp = self._make_dispatcher(redis)
        disp._judge_pointer = 0

        result = asyncio.run(disp._choose_daemon())
        assert result == "B"

    def test_empty_daemon_set_returns_none(self):
        redis = _fake_redis(smembers_result=set())
        disp = self._make_dispatcher(redis)

        result = asyncio.run(disp._choose_daemon())
        assert result is None

    def test_pointer_clamped_on_daemon_removal(self):
        """Pointer clamped to 0 if it exceeds the daemon list size."""
        redis = _fake_redis(
            smembers_result={b"A"},
            hgetall_map={
                "worker-daemon:A": _make_daemon_hash(free_slots=2),
            },
        )
        disp = self._make_dispatcher(redis)
        disp._judge_pointer = 5

        result = asyncio.run(disp._choose_daemon())
        assert result == "A"


# ── DistributedDispatcher — dispatch flow ──────────────────────────

class TestSpawn:
    def _make_dispatcher(self, redis):
        from orchestrator.dispatcher import DistributedDispatcher
        from orchestrator.registry import WorkerRegistry
        reg = WorkerRegistry()
        return DistributedDispatcher(redis, reg, cache_ttl=0)

    @pytest.mark.asyncio
    async def test_spawn_dispatches_to_daemon_inbox(self):
        """spawn() XADDs the task fields to the correct inbox key."""
        redis = _fake_redis(
            smembers_result={b"A"},
            hgetall_map={
                "worker-daemon:A": _make_daemon_hash(free_slots=3),
            },
        )
        disp = self._make_dispatcher(redis)

        await disp.spawn("bug-abc", "42", "https://g/org/repo", "99", "feature/x")

        redis.xadd.assert_called()
        call_args = redis.xadd.call_args
        inbox_key = call_args[0][0]
        assert "worker-daemon:A:inbox" == inbox_key

    @pytest.mark.asyncio
    async def test_spawn_all_full_enqueues_pending(self):
        """When all daemons are full, task goes to pending."""
        redis = _fake_redis(
            smembers_result={b"A"},
            hgetall_map={
                "worker-daemon:A": _make_daemon_hash(free_slots=0),
            },
        )
        disp = self._make_dispatcher(redis)

        await disp.spawn("bug-abc", "42", "url", "99", "")

        all_xadd_calls = [c[0][0] for c in redis.xadd.call_args_list
                          if c[0]]
        assert "worker-daemon:pending" in all_xadd_calls


# ── DistributedDispatcher — restart ────────────────────────────────

class TestRestart:
    def test_restart_is_noop(self):
        """restart() is a no-op in distr-pull — daemon restart is external."""
        redis = _fake_redis()
        from orchestrator.dispatcher import DistributedDispatcher
        from orchestrator.registry import WorkerRegistry
        disp = DistributedDispatcher(redis, WorkerRegistry())
        disp.restart("bug1", "p", "u", "j", "")
        redis.xadd.assert_not_called()
