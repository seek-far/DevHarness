"""
DistributedDispatcher: round-robin task dispatch for distr-pull mode.

Replaces the orchestrator's WorkerSpawner when WORKER_SPAWNER=distr-pull.
Instead of spawning a subprocess / container / k8s Job, it XADDs the task
to a daemon's Redis inbox Stream.

Daemon selection is round-robin with resource awareness — daemons that are
at capacity (free_slots==0 or low memory) are skipped so the task lands on
the first daemon that can accept it.  When all daemons are full, the task
goes to a pending queue for later retry by DaemonMonitor.

The spawn() / restart() signatures match the existing WorkerSpawner calling
convention so _handle_message() needs no changes.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from redis.asyncio import Redis

from .registry import WorkerRegistry

logger = logging.getLogger(__name__)

# Key templates
INBOX_KEY = "worker-daemon:{daemon_id}:inbox"
PENDING_STREAM = "worker-daemon:pending"
DAEMONS_SET = "worker-daemons"
DAEMON_HASH = "worker-daemon:{daemon_id}"

# Minimum free memory (MB) a daemon must have to be eligible.
# Aligned with the daemon's own --min-free-memory-mb default (2048).
DEFAULT_MIN_FREE_MEMORY_MB = 2048


class DaemonInfo:
    """In-memory snapshot of a daemon's latest heartbeat."""

    __slots__ = (
        "daemon_id", "free_slots", "free_memory_mb",
        "hostname", "concurrency", "last_hb_ts",
    )

    def __init__(
        self,
        daemon_id: str,
        free_slots: int = 0,
        free_memory_mb: int = 0,
        hostname: str = "",
        concurrency: int = 0,
        last_hb_ts: int = 0,
    ):
        self.daemon_id = daemon_id
        self.free_slots = free_slots
        self.free_memory_mb = free_memory_mb
        self.hostname = hostname
        self.concurrency = concurrency
        self.last_hb_ts = last_hb_ts

    @classmethod
    def from_hash(cls, daemon_id: str, raw: dict[bytes, bytes]) -> "DaemonInfo":
        """Parse a Redis Hash (bytes→bytes) into a DaemonInfo."""
        def _b(key: str) -> int:
            return int(raw.get(key.encode(), b"0"))

        return cls(
            daemon_id=daemon_id,
            free_slots=_b("free_slots"),
            free_memory_mb=_b("free_memory_mb"),
            hostname=raw.get(b"hostname", b"").decode(errors="replace"),
            concurrency=_b("concurrency"),
            last_hb_ts=_b("last_hb_ts"),
        )

    @property
    def is_alive(self) -> bool:
        """A daemon whose last heartbeat is more recent than TTL is alive."""
        return self.last_hb_ts > 0

    def can_accept(self, min_free_memory_mb: int = DEFAULT_MIN_FREE_MEMORY_MB) -> bool:
        """Can this daemon accept a new task?"""
        return (
            self.free_slots > 0
            and self.free_memory_mb >= min_free_memory_mb
        )


class DistributedDispatcher:
    """
    Round-robin task dispatcher for the distr-pull spawner mode.

    duck-types WorkerSpawner — implements spawn() / restart() with the
    same signatures so orchestrator._handle_message() is unchanged.
    """

    def __init__(
        self,
        redis: Redis,
        registry: WorkerRegistry,
        cache_ttl: int = 5,
        min_free_memory_mb: int = DEFAULT_MIN_FREE_MEMORY_MB,
    ):
        self._redis = redis
        self._registry = registry          # kept for interface compat; not used
        self._cache_ttl = cache_ttl
        self._min_free_memory_mb = min_free_memory_mb

        # Round-robin pointer — index into the sorted daemon ID list.
        self._judge_pointer: int = 0

        # Cached daemon state.
        self._daemon_cache: dict[str, DaemonInfo] = {}
        self._cache_fetched_at: float = 0.0  # monotonic timestamp

    # ── public interface (duck-types WorkerSpawner) ──────────────

    async def spawn(
        self,
        bug_id: str,
        project_id: str,
        project_web_url: str,
        job_id: str,
        source_branch: str = "",
    ) -> None:
        """
        Dispatch a task to a daemon, or enqueue to pending if none can accept.

        Returns None (distr-pull does not create WorkerEntry objects).
        """
        daemon_id = await self._choose_daemon()
        if daemon_id is not None:
            await self._dispatch_to(daemon_id, bug_id, project_id,
                                    project_web_url, job_id, source_branch)
        else:
            await self._enqueue_pending(bug_id, project_id,
                                        project_web_url, job_id, source_branch)

    async def restart(self, bug_id: str, project_id: str,
                      project_web_url: str, job_id: str,
                      source_branch: str = "") -> None:
        """
        Daemon restart is out of scope for distr-pull — delegated to systemd /
        container runtime / K8s.  This method is a no-op; it exists only to
        satisfy the WorkerSpawner duck-type that HealthMonitor may call.
        """
        logger.debug("distr-pull restart no-op for bug_id=%s", bug_id)

    # ── daemon selection ─────────────────────────────────────────

    async def _choose_daemon(self) -> Optional[str]:
        """Round-robin: return the first daemon that can accept, or None."""
        await self._maybe_refresh_cache()

        daemon_ids = sorted(self._daemon_cache.keys())
        if not daemon_ids:
            logger.warning("[Dispatcher] no daemons registered")
            return None

        n = len(daemon_ids)
        # Clamp pointer in case daemons were removed.
        if self._judge_pointer >= n:
            self._judge_pointer = 0

        for offset in range(n):
            idx = (self._judge_pointer + offset) % n
            daemon_id = daemon_ids[idx]
            info = self._daemon_cache.get(daemon_id)

            if info is None:
                continue

            if not info.is_alive:
                logger.debug("[Dispatcher] daemon=%s dead (no heartbeat), skip",
                             daemon_id)
                continue

            if info.can_accept(self._min_free_memory_mb):
                # Advance pointer to the NEXT daemon (the one *after* this
                # assignment) so the daemon that just took a task moves to
                # the back of the preference order.
                self._judge_pointer = (idx + 1) % n
                logger.info(
                    "[Dispatcher] assigned bug_id to daemon=%s "
                    "free_slots=%d free_mem=%dMB pointer=%d",
                    daemon_id, info.free_slots, info.free_memory_mb,
                    self._judge_pointer,
                )
                return daemon_id

            logger.debug(
                "[Dispatcher] daemon=%s skip — free_slots=%d free_mem=%dMB",
                daemon_id, info.free_slots, info.free_memory_mb,
            )

        # All daemons at capacity. Advance pointer by 1 so the next dispatch
        # starts from a different daemon (fairness across full→free transitions).
        self._judge_pointer = (self._judge_pointer + 1) % n if n else 0
        logger.warning("[Dispatcher] all daemons full, enqueuing to pending")
        return None

    async def _maybe_refresh_cache(self) -> None:
        """Refresh the daemon cache if stale."""
        now = time.monotonic()
        if now - self._cache_fetched_at < self._cache_ttl:
            return

        daemon_ids: list[str] = []
        raw = await self._redis.smembers(DAEMONS_SET)
        for member in raw:
            daemon_ids.append(member.decode() if isinstance(member, bytes) else member)

        new_cache: dict[str, DaemonInfo] = {}
        for daemon_id in daemon_ids:
            hash_key = DAEMON_HASH.format(daemon_id=daemon_id)
            data = await self._redis.hgetall(hash_key)
            if data:
                new_cache[daemon_id] = DaemonInfo.from_hash(daemon_id, data)
            else:
                # Hash key expired or never written — stale entry in the Set.
                logger.debug("[Dispatcher] stale daemon Set entry: %s", daemon_id)

        self._daemon_cache = new_cache
        self._cache_fetched_at = now

    # ── dispatch helpers ─────────────────────────────────────────

    async def _dispatch_to(
        self,
        daemon_id: str,
        bug_id: str,
        project_id: str,
        project_web_url: str,
        job_id: str,
        source_branch: str,
    ) -> None:
        """XADD a task to a daemon's inbox Stream."""
        inbox_key = INBOX_KEY.format(daemon_id=daemon_id)
        fields = {
            "bug_id": bug_id,
            "project_id": project_id,
            "project_web_url": project_web_url,
            "job_id": job_id,
            "source_branch": source_branch or "",
        }
        # Ensure the Stream and consumer group exist (idempotent).
        try:
            await self._redis.xgroup_create(
                inbox_key, "daemon-group", mkstream=True,
            )
        except Exception:
            pass  # BUSYGROUP or already exists — ignore

        await self._redis.xadd(inbox_key, fields)
        logger.info("[Dispatcher] XADD daemon=%s inbox=%s bug_id=%s",
                    daemon_id, inbox_key, bug_id)

    async def _enqueue_pending(
        self,
        bug_id: str,
        project_id: str,
        project_web_url: str,
        job_id: str,
        source_branch: str,
    ) -> None:
        """All daemons full — push to the pending queue for later retry."""
        try:
            await self._redis.xgroup_create(
                PENDING_STREAM, "orch-group", mkstream=True,
            )
        except Exception:
            pass

        await self._redis.xadd(PENDING_STREAM, {
            "bug_id": bug_id,
            "project_id": project_id,
            "project_web_url": project_web_url,
            "job_id": job_id,
            "source_branch": source_branch or "",
        })
        logger.info("[Dispatcher] enqueued to pending bug_id=%s", bug_id)

    # ── helpers for DaemonMonitor ────────────────────────────────

    async def spawn_raw(self, fields: dict[str, str]) -> None:
        """
        Re-dispatch a task from raw fields (recovered from pending / rejected /
        dead-daemon inbox).  Used by DaemonMonitor.
        """
        daemon_id = await self._choose_daemon()
        if daemon_id is not None:
            await self._dispatch_to(
                daemon_id,
                fields["bug_id"],
                fields.get("project_id", ""),
                fields.get("project_web_url", ""),
                fields.get("job_id", ""),
                fields.get("source_branch", ""),
            )
        else:
            # Still full — re-enqueue to pending.
            await self._redis.xadd(PENDING_STREAM, fields)
            logger.debug("[Dispatcher] re-enqueued to pending: bug_id=%s",
                         fields.get("bug_id"))

    def get_cached_daemon_ids(self) -> list[str]:
        """Return currently cached (alive) daemon IDs.  For DaemonMonitor."""
        return sorted(self._daemon_cache.keys())
