"""
Worker daemon for distr-pull mode.

A long-lived process that runs on each host machine.  It:
  - Registers with Redis (SADD worker-daemons).
  - Sends periodic heartbeats (HSET + EXPIRE on worker-daemon:{id}).
  - Reads tasks from its personal inbox Stream (XREADGROUP).
  - Checks local resources (free slots + available memory).
  - Spawns ``bf_worker.py`` as a subprocess per accepted task.
  - Reaps completed workers and frees slots.
  - Rejects tasks it cannot handle → worker-daemon:rejected.
  - On SIGTERM: drains active workers (up to 600 s), then force-kills.

Usage::

    python -m bf_worker.worker_daemon \
        --daemon-id A \
        --concurrency 4 \
        --min-free-memory-mb 2048

``bf_worker.py`` is never modified — the daemon spawns it as a subprocess
with the same CLI and environment variables the orchestrator's
WorkerSpawner uses today.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Optional

import redis.asyncio as aioredis

# Ensure the repo root is on sys.path so that bf_worker.py (and its imports)
# resolve correctly when spawned as a subprocess.  bf_worker.py itself does
# the same at line 29 — we do it here first so the daemon's own startup logging
# works.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logger = logging.getLogger("worker_daemon")

# Path to the worker entry-point script (same as WorkerSpawner).
WORKER_SCRIPT = str(_REPO_ROOT / "bf_worker" / "bf_worker.py")

# ── Redis key templates ─────────────────────────────────────────────

DAEMONS_SET = "worker-daemons"
DAEMON_HASH = "worker-daemon:{daemon_id}"
INBOX_KEY = "worker-daemon:{daemon_id}:inbox"
REJECTED_STREAM = "worker-daemon:rejected"
TASK_OWNER_KEY = "task:owner:{bug_id}"
DAEMON_GROUP = "daemon-group"

# Hard limits for graceful shutdown.
DRAIN_TIMEOUT = 600       # seconds to wait for active workers to exit
FORCE_KILL_TIMEOUT = 10   # seconds after SIGTERM before SIGKILL


# ── process handle ───────────────────────────────────────────────────

class _ProcessHandle:
    """Wrapper around an asyncio subprocess handle."""

    __slots__ = ("proc", "bug_id", "started_at")

    def __init__(self, proc: asyncio.subprocess.Process, bug_id: str):
        self.proc = proc
        self.bug_id = bug_id
        self.started_at = time.monotonic()


# ── daemon ────────────────────────────────────────────────────────────

class WorkerDaemon:
    """Long-lived daemon that pulls tasks and spawns workers on-demand."""

    def __init__(
        self,
        daemon_id: str,
        redis_url: str,
        concurrency: int = 4,
        min_free_memory_mb: int = 2048,
        hb_interval: int = 10,
        hb_ttl: int = 30,
    ):
        self.daemon_id = daemon_id
        self._redis_url = redis_url
        self._concurrency = concurrency
        self._min_free_memory_bytes = min_free_memory_mb * 2**20
        self._hb_interval = hb_interval
        self._hb_ttl = hb_ttl

        self._redis: Optional[aioredis.Redis] = None
        self._active: dict[str, _ProcessHandle] = {}
        self._running = False

        # Inbox key for this daemon.
        self._inbox_key = INBOX_KEY.format(daemon_id=daemon_id)
        self._hash_key = DAEMON_HASH.format(daemon_id=daemon_id)

    # ── public API ──────────────────────────────────────────────────

    async def run(self) -> None:
        """Main entry point. Blocks until SIGTERM / SIGINT."""
        self._running = True
        self._redis = aioredis.from_url(
            self._redis_url, decode_responses=False,
            socket_timeout=30, socket_connect_timeout=10,
        )

        await self._register()

        hb_task = asyncio.create_task(self._heartbeat_loop(), name="daemon-hb")
        inbox_task = asyncio.create_task(self._inbox_loop(), name="daemon-inbox")

        logger.info("daemon %s started (concurrency=%d min_mem=%dMB)",
                     self.daemon_id, self._concurrency,
                     self._min_free_memory_bytes // 2**20)

        # Wait for shutdown signal.
        loop = asyncio.get_running_loop()
        stop: asyncio.Future = loop.create_future()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(
                    sig,
                    lambda: stop.done() or stop.set_result(None),
                )
            except NotImplementedError:
                pass

        await stop
        logger.info("daemon %s received stop signal", self.daemon_id)

        # Cancel background loops.
        for task in (inbox_task, hb_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await self._shutdown()
        if self._redis:
            await self._redis.aclose()

    # ── registration ────────────────────────────────────────────────

    async def _register(self) -> None:
        """Register this daemon in Redis and ensure inbox consumer group."""
        assert self._redis is not None
        await self._redis.sadd(DAEMONS_SET, self.daemon_id)

        # Ensure the consumer group for this daemon's inbox exists.
        try:
            await self._redis.xgroup_create(
                self._inbox_key, DAEMON_GROUP, mkstream=True,
            )
        except Exception:
            pass  # BUSYGROUP

        # First heartbeat immediately so orchestrator sees us right away.
        await self._write_heartbeat()

    # ── heartbeat ───────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Periodically refresh the daemon heartbeat Hash."""
        assert self._redis is not None
        while self._running:
            try:
                await self._write_heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("daemon %s heartbeat write failed", self.daemon_id)
            await asyncio.sleep(self._hb_interval)

    async def _write_heartbeat(self) -> None:
        """Write one heartbeat Hash and set TTL."""
        assert self._redis is not None
        mem = self._get_free_memory_mb()
        free_slots = self._free_slots()

        await self._redis.hset(self._hash_key, mapping={
            "free_slots": str(free_slots),
            "free_memory_mb": str(mem),
            "hostname": socket.gethostname(),
            "concurrency": str(self._concurrency),
            "last_hb_ts": str(time.time_ns() // 1_000_000),
        })
        await self._redis.expire(self._hash_key, self._hb_ttl)

    # ── inbox loop ──────────────────────────────────────────────────

    async def _inbox_loop(self) -> None:
        """
        Continuously read from this daemon's inbox.
        If we can accept → spawn worker.
        If we cannot → reject the task back to the rejected stream.
        """
        assert self._redis is not None
        while self._running:
            try:
                msgs = await self._redis.xreadgroup(
                    groupname=DAEMON_GROUP,
                    consumername=self.daemon_id,
                    streams={self._inbox_key: ">"},
                    count=1,
                    block=5000,  # 5 s — responsive to shutdown
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("daemon %s inbox read failed", self.daemon_id)
                await asyncio.sleep(1)
                continue

            if not msgs:
                continue

            for _stream_name, entries in msgs:
                for msg_id, fields_raw in entries:
                    task = self._parse_task(fields_raw)

                    if self._can_accept():
                        await self._accept(msg_id, task)
                    else:
                        await self._reject(msg_id, task)

    # ── resource checks ─────────────────────────────────────────────

    def _free_slots(self) -> int:
        return self._concurrency - len(self._active)

    def _get_free_memory_mb(self) -> int:
        """Return available system memory in MB (psutil)."""
        try:
            import psutil
            return psutil.virtual_memory().available // 2**20
        except Exception:
            # psutil unavailable — report a high value so the memory check
            # is effectively a no-op (the orchestrator or operator should
            # configure --min-free-memory-mb accordingly).
            return 10_000_000

    def _get_free_memory_bytes(self) -> int:
        try:
            import psutil
            return psutil.virtual_memory().available
        except Exception:
            return 10_000_000 * 2**20

    def _can_accept(self) -> bool:
        """Return True if we have free slots and enough memory."""
        return (
            self._free_slots() > 0
            and self._get_free_memory_bytes() >= self._min_free_memory_bytes
        )

    # ── task acceptance / rejection ─────────────────────────────────

    @staticmethod
    def _parse_task(fields_raw: dict) -> dict[str, str]:
        """Decode raw Redis field bytes into a string dict."""
        task: dict[str, str] = {}
        for k, v in fields_raw.items():
            key = k.decode() if isinstance(k, bytes) else k
            val = v.decode() if isinstance(v, bytes) else v
            task[key] = val
        return task

    async def _accept(self, msg_id: bytes | str, task: dict[str, str]) -> None:
        """Spawn a worker subprocess for this task."""
        assert self._redis is not None
        bug_id = task["bug_id"]

        logger.info("daemon %s accepting bug_id=%s (free_slots=%d → %d)",
                     self.daemon_id, bug_id, self._free_slots(),
                     self._free_slots() - 1)

        # Mark ownership in Redis.
        owner_key = TASK_OWNER_KEY.format(bug_id=bug_id)
        await self._redis.set(owner_key, self.daemon_id, ex=3600)

        # Spawn the worker subprocess.
        env = os.environ.copy()
        env["REDIS_URL"] = self._redis_url
        env["BUG_ID"] = bug_id
        env["project_id"] = task.get("project_id", "")
        env["project_web_url"] = task.get("project_web_url", "")
        env["job_id"] = task.get("job_id", "")
        env["BUG_SOURCE_BRANCH"] = task.get("source_branch", "") or ""
        if os.getenv("BF_AGENT_CONFIG"):
            env["BF_AGENT_CONFIG"] = os.environ["BF_AGENT_CONFIG"]

        proc = await asyncio.create_subprocess_exec(
            sys.executable, WORKER_SCRIPT, "--bug-id", bug_id,
            env=env,
        )
        handle = _ProcessHandle(proc, bug_id)
        self._active[bug_id] = handle

        logger.info("daemon %s spawned worker pid=%s bug_id=%s",
                     self.daemon_id, proc.pid, bug_id)

        # ACK the inbox message.
        await self._redis.xack(self._inbox_key, DAEMON_GROUP, msg_id)

        # Background reaping.
        asyncio.create_task(self._reap(bug_id))

    async def _reject(self, msg_id: bytes | str, task: dict[str, str]) -> None:
        """Reject a task — push to the rejected stream for retry."""
        assert self._redis is not None
        bug_id = task.get("bug_id", "?")

        reason = (
            "no_free_slots" if self._free_slots() == 0
            else "insufficient_memory"
        )
        logger.warning(
            "daemon %s rejecting bug_id=%s reason=%s free_slots=%d free_mem=%dMB",
            self.daemon_id, bug_id, reason, self._free_slots(),
            self._get_free_memory_mb(),
        )

        try:
            await self._redis.xgroup_create(
                REJECTED_STREAM, "orch-group", mkstream=True,
            )
        except Exception:
            pass

        await self._redis.xadd(REJECTED_STREAM, {
            "daemon_id": self.daemon_id,
            "bug_id": task.get("bug_id", ""),
            "project_id": task.get("project_id", ""),
            "project_web_url": task.get("project_web_url", ""),
            "job_id": task.get("job_id", ""),
            "source_branch": task.get("source_branch", ""),
            "reason": reason,
        })
        await self._redis.xack(self._inbox_key, DAEMON_GROUP, msg_id)

    # ── worker reaping ──────────────────────────────────────────────

    async def _reap(self, bug_id: str) -> None:
        """Wait for a worker subprocess to exit, then free its slot."""
        assert self._redis is not None

        handle = self._active.get(bug_id)
        if handle is None:
            return

        try:
            rc = await handle.proc.wait()
        except Exception:
            rc = -1

        elapsed = time.monotonic() - handle.started_at
        logger.info("daemon %s worker exited bug_id=%s rc=%d elapsed=%.0fs",
                     self.daemon_id, bug_id, rc, elapsed)

        del self._active[bug_id]

        # Clear ownership.
        owner_key = TASK_OWNER_KEY.format(bug_id=bug_id)
        try:
            await self._redis.delete(owner_key)
        except Exception:
            pass

    # ── graceful shutdown ───────────────────────────────────────────

    async def _shutdown(self) -> None:
        """Drain active workers, then exit."""
        assert self._redis is not None

        # Deregister so orchestrator stops sending tasks.
        logger.info("daemon %s deregistering (active=%d)",
                     self.daemon_id, len(self._active))
        try:
            await self._redis.srem(DAEMONS_SET, self.daemon_id)
            await self._redis.delete(self._hash_key)
        except Exception:
            pass

        if not self._active:
            logger.info("daemon %s shutdown complete (no active workers)",
                         self.daemon_id)
            return

        # Wait for active workers to finish.
        deadline = time.monotonic() + DRAIN_TIMEOUT
        while self._active and time.monotonic() < deadline:
            logger.info("daemon %s draining... %d workers remaining: %s",
                         self.daemon_id, len(self._active),
                         list(self._active.keys()))
            await asyncio.sleep(5)

        if not self._active:
            logger.info("daemon %s shutdown complete (all workers drained)",
                         self.daemon_id)
            return

        # Timeout — force-kill remaining workers.
        logger.warning(
            "daemon %s drain timeout (%ds) with %d workers still running: %s. "
            "Force terminating.",
            self.daemon_id, DRAIN_TIMEOUT, len(self._active),
            list(self._active.keys()),
        )

        for bug_id, handle in list(self._active.items()):
            logger.warning("daemon %s force killing worker bug_id=%s pid=%s",
                           self.daemon_id, bug_id, handle.proc.pid)
            try:
                handle.proc.terminate()
                await asyncio.wait_for(handle.proc.wait(),
                                       timeout=FORCE_KILL_TIMEOUT)
            except asyncio.TimeoutError:
                try:
                    handle.proc.kill()
                except Exception:
                    pass
                logger.warning("daemon %s killed worker bug_id=%s pid=%s (SIGKILL)",
                               self.daemon_id, bug_id, handle.proc.pid)
            except Exception as e:
                logger.error("daemon %s error killing worker bug_id=%s: %s",
                             self.daemon_id, bug_id, e)

            # Clear ownership even on force-kill.
            owner_key = TASK_OWNER_KEY.format(bug_id=bug_id)
            try:
                await self._redis.delete(owner_key)
            except Exception:
                pass

        logger.info("daemon %s shutdown complete (force-killed)", self.daemon_id)


# ── entry point ─────────────────────────────────────────────────────────

async def _main() -> None:
    parser = argparse.ArgumentParser(
        description="SDLCMA Worker Daemon (distr-pull mode)",
    )
    parser.add_argument(
        "--daemon-id", required=True,
        help="Unique daemon identifier, e.g. A or host1",
    )
    parser.add_argument(
        "--concurrency", type=int, default=4,
        help="Maximum concurrent worker subprocesses (default: 4)",
    )
    parser.add_argument(
        "--min-free-memory-mb", type=int, default=2048,
        help="Minimum free memory (MB) before accepting a task (default: 2048)",
    )
    parser.add_argument(
        "--redis-url", default="",
        help="Redis URL (default: from REDIS_URL env)",
    )
    parser.add_argument(
        "--hb-interval", type=int, default=10,
        help="Heartbeat interval in seconds (default: 10)",
    )
    parser.add_argument(
        "--hb-ttl", type=int, default=30,
        help="Heartbeat key TTL in seconds (default: 30)",
    )
    args = parser.parse_args()

    redis_url = args.redis_url or os.environ.get("REDIS_URL", "redis://localhost:6379/0")

    daemon = WorkerDaemon(
        daemon_id=args.daemon_id,
        redis_url=redis_url,
        concurrency=args.concurrency,
        min_free_memory_mb=args.min_free_memory_mb,
        hb_interval=args.hb_interval,
        hb_ttl=args.hb_ttl,
    )
    await daemon.run()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s [daemon %(name)s:%(funcName)s:%(lineno)d] %(message)s",
        stream=sys.stdout,
    )
    asyncio.run(_main())
