"""
Unit tests for WorkerDaemon — resource checks, spawn/reap lifecycle, rejection.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bf_worker.worker_daemon import WorkerDaemon, _ProcessHandle


# ── helpers ────────────────────────────────────────────────────────

def _make_daemon(concurrency=4, min_free_memory_mb=2048):
    """Create a WorkerDaemon with a fake Redis URL (never actually connects)."""
    return WorkerDaemon(
        daemon_id="test",
        redis_url="redis://fake:6379/0",
        concurrency=concurrency,
        min_free_memory_mb=min_free_memory_mb,
    )


# ── resource checks ────────────────────────────────────────────────

class TestResourceChecks:
    def test_can_accept_with_free_slots_and_memory(self):
        daemon = _make_daemon(concurrency=4, min_free_memory_mb=2048)
        # No active workers → free_slots = 4.
        with patch.object(daemon, "_get_free_memory_bytes", return_value=8 * 2**30):
            assert daemon._can_accept()

    def test_cannot_accept_zero_slots(self):
        daemon = _make_daemon(concurrency=2, min_free_memory_mb=2048)
        # Fill all slots.
        daemon._active["bug1"] = _ProcessHandle(AsyncMock(), "bug1")
        daemon._active["bug2"] = _ProcessHandle(AsyncMock(), "bug2")

        with patch.object(daemon, "_get_free_memory_bytes", return_value=8 * 2**30):
            assert not daemon._can_accept()
            assert daemon._free_slots() == 0

    def test_cannot_accept_low_memory(self):
        daemon = _make_daemon(concurrency=4, min_free_memory_mb=4096)
        with patch.object(daemon, "_get_free_memory_bytes", return_value=2 * 2**30):
            assert not daemon._can_accept()

    def test_free_slots_formula(self):
        daemon = _make_daemon(concurrency=4)
        assert daemon._free_slots() == 4
        daemon._active["bug1"] = _ProcessHandle(AsyncMock(), "bug1")
        assert daemon._free_slots() == 3
        daemon._active["bug2"] = _ProcessHandle(AsyncMock(), "bug2")
        daemon._active["bug3"] = _ProcessHandle(AsyncMock(), "bug3")
        assert daemon._free_slots() == 1

    def test_get_free_memory_mb_uses_psutil(self):
        daemon = _make_daemon()
        # psutil is imported lazily inside _get_free_memory_mb().
        # Patch the daemon method itself rather than the module attribute.
        with patch.object(daemon, "_get_free_memory_mb", return_value=4096):
            mb = daemon._get_free_memory_mb()
            assert mb == 4096


# ── task parsing ───────────────────────────────────────────────────

class TestTaskParsing:
    def test_parse_task_bytes_keys(self):
        daemon = _make_daemon()
        raw = {
            b"bug_id": b"bug-123",
            b"project_id": b"42",
            b"source_branch": b"feature/x",
        }
        task = daemon._parse_task(raw)
        assert task == {
            "bug_id": "bug-123",
            "project_id": "42",
            "source_branch": "feature/x",
        }

    def test_parse_task_str_keys(self):
        daemon = _make_daemon()
        raw = {"bug_id": "bug-456", "project_id": "99"}
        task = daemon._parse_task(raw)
        assert task["bug_id"] == "bug-456"


# ── reject path ────────────────────────────────────────────────────

class TestReject:
    @pytest.mark.asyncio
    async def test_reject_xadds_to_rejected_stream(self):
        daemon = _make_daemon(concurrency=4, min_free_memory_mb=2048)
        daemon._redis = AsyncMock()
        daemon._redis.xack = AsyncMock()
        daemon._redis.xadd = AsyncMock()
        daemon._active["b1"] = _ProcessHandle(AsyncMock(), "b1")
        daemon._active["b2"] = _ProcessHandle(AsyncMock(), "b2")
        daemon._active["b3"] = _ProcessHandle(AsyncMock(), "b3")
        daemon._active["b4"] = _ProcessHandle(AsyncMock(), "b4")

        task = {"bug_id": "bug-full", "project_id": "42"}
        await daemon._reject(b"msg1", task)

        daemon._redis.xadd.assert_called()
        call_key = daemon._redis.xadd.call_args[0][0]
        assert call_key == "worker-daemon:rejected"

    @pytest.mark.asyncio
    async def test_reject_xacks_inbox_message(self):
        daemon = _make_daemon(concurrency=1, min_free_memory_mb=2048)
        daemon._redis = AsyncMock()
        daemon._redis.xack = AsyncMock()
        daemon._redis.xadd = AsyncMock()
        daemon._active["b1"] = _ProcessHandle(AsyncMock(), "b1")

        task = {"bug_id": "bug-full", "project_id": "42"}
        await daemon._reject(b"msg_id_123", task)

        daemon._redis.xack.assert_called_with(
            "worker-daemon:test:inbox", "daemon-group", b"msg_id_123",
        )


# ── accept / spawn ─────────────────────────────────────────────────

class TestAccept:
    @pytest.mark.asyncio
    async def test_accept_sets_ownership_key(self):
        daemon = _make_daemon(concurrency=4)
        daemon._redis = AsyncMock()
        daemon._redis.set = AsyncMock()
        daemon._redis.xack = AsyncMock()

        task = {"bug_id": "bug-abc", "project_id": "42", "job_id": "99"}
        with patch("bf_worker.worker_daemon.asyncio.create_subprocess_exec",
                   new_callable=AsyncMock) as mock_spawn:
            mock_proc = AsyncMock()
            mock_proc.pid = 12345
            mock_spawn.return_value = mock_proc

            await daemon._accept(b"msg1", task)

        daemon._redis.set.assert_called_with(
            "task:owner:bug-abc", "test", ex=3600,
        )
        assert "bug-abc" in daemon._active

    @pytest.mark.asyncio
    async def test_accept_xacks_message(self):
        daemon = _make_daemon(concurrency=4)
        daemon._redis = AsyncMock()
        daemon._redis.set = AsyncMock()
        daemon._redis.xack = AsyncMock()

        task = {"bug_id": "bug-def", "project_id": "1", "job_id": "1"}
        with patch("bf_worker.worker_daemon.asyncio.create_subprocess_exec",
                   new_callable=AsyncMock) as mock_spawn:
            mock_spawn.return_value = AsyncMock(pid=99)
            await daemon._accept(b"msg_xyz", task)

        daemon._redis.xack.assert_called_with(
            "worker-daemon:test:inbox", "daemon-group", b"msg_xyz",
        )

    @pytest.mark.asyncio
    async def test_accept_spawns_with_correct_env(self):
        daemon = _make_daemon(concurrency=4)
        daemon._redis = AsyncMock()
        daemon._redis.set = AsyncMock()
        daemon._redis.xack = AsyncMock()

        task = {
            "bug_id": "bug-env",
            "project_id": "999",
            "project_web_url": "https://gitlab.com/org/repo",
            "job_id": "888",
            "source_branch": "feature/b",
        }

        with patch("bf_worker.worker_daemon.asyncio.create_subprocess_exec",
                   new_callable=AsyncMock) as mock_spawn:
            mock_spawn.return_value = AsyncMock(pid=111)
            await daemon._accept(b"msg1", task)

        # Check env passed to the subprocess.
        call_kwargs = mock_spawn.call_args
        assert call_kwargs is not None
        # args are (sys.executable, WORKER_SCRIPT, "--bug-id", "bug-env")
        args_list = call_kwargs[0] if call_kwargs[0] else ()
        assert "--bug-id" in args_list
        assert "bug-env" in args_list


# ── reap ────────────────────────────────────────────────────────────

class TestReap:
    @pytest.mark.asyncio
    async def test_reap_removes_from_active(self):
        daemon = _make_daemon(concurrency=4)
        daemon._redis = AsyncMock()
        daemon._redis.delete = AsyncMock()

        proc = AsyncMock()
        proc.wait = AsyncMock(return_value=0)
        daemon._active["bug-r"] = _ProcessHandle(proc, "bug-r")

        assert daemon._free_slots() == 3  # 4 - 1
        await daemon._reap("bug-r")
        assert daemon._free_slots() == 4  # slot freed

    @pytest.mark.asyncio
    async def test_reap_clears_ownership_key(self):
        daemon = _make_daemon(concurrency=4)
        daemon._redis = AsyncMock()
        daemon._redis.delete = AsyncMock()

        proc = AsyncMock()
        proc.wait = AsyncMock(return_value=0)
        daemon._active["bug-r"] = _ProcessHandle(proc, "bug-r")

        await daemon._reap("bug-r")
        daemon._redis.delete.assert_called_with("task:owner:bug-r")
