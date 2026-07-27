"""
DaemonMonitor: background recovery loop for distr-pull mode.

Runs in the orchestrator process as a single asyncio.Task that ticks every
``worker_daemon_recovery_interval`` seconds.  Three responsibilities per tick:

1. Dead-daemon recovery — find daemons whose heartbeat Hash has expired,
   recover their unacked inbox messages (XPENDING), and re-dispatch them.

2. Pending queue retry — tasks that were enqueued because all daemons were
   full are retried via the dispatcher.

3. Rejected task retry — tasks that a daemon explicitly rejected are retried
   via the dispatcher (which will naturally skip the rejecting daemon if it
   is still full).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from redis.asyncio import Redis

if TYPE_CHECKING:
    from .dispatcher import DistributedDispatcher

logger = logging.getLogger(__name__)

# Key constants — shared with dispatcher.py; duplicated here to keep modules
# independently readable.
DAEMONS_SET = "worker-daemons"
DAEMON_HASH = "worker-daemon:{daemon_id}"
INBOX_KEY = "worker-daemon:{daemon_id}:inbox"
PENDING_STREAM = "worker-daemon:pending"
REJECTED_STREAM = "worker-daemon:rejected"

# Consumer group names
DAEMON_GROUP = "daemon-group"
ORCH_GROUP = "orch-group"


class DaemonMonitor:
    """
    Background monitor that recovers tasks from dead daemons and retries
    pending / rejected tasks.

    Does NOT monitor individual worker subprocesses — that is the daemon's
    responsibility.  Does NOT restart daemon processes — that is the operator's
    responsibility (systemd / container runtime / K8s).
    """

    def __init__(
        self,
        redis: Redis,
        dispatcher: "DistributedDispatcher",
        interval: int = 15,
    ):
        self._redis = redis
        self._dispatcher = dispatcher
        self._interval = interval
        self._running = False

    def start(self) -> None:
        """Start the background tick loop."""
        import asyncio
        self._running = True
        self._task = asyncio.get_event_loop().create_task(
            self._run(), name="DaemonMonitor"
        )
        logger.info("[DaemonMonitor] started (interval=%ds)", self._interval)

    async def stop(self) -> None:
        """Cancel the background tick loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[DaemonMonitor] stopped")

    async def _run(self) -> None:
        """Main tick loop."""
        import asyncio
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[DaemonMonitor] tick error — will retry")
            await asyncio.sleep(self._interval)

    async def _tick(self) -> None:
        """One pass through all three recovery responsibilities."""
        await self._recover_dead_daemons()
        await self._retry_pending()
        await self._retry_rejected()

    # ── dead-daemon recovery ─────────────────────────────────────

    async def _recover_dead_daemons(self) -> None:
        """
        Find daemons whose heartbeat Hash has expired.
        Recover their unacked inbox messages and re-dispatch.
        Clean up their Redis keys.
        """
        daemon_ids: list[str] = []
        raw = await self._redis.smembers(DAEMONS_SET)
        for member in raw:
            daemon_ids.append(member.decode() if isinstance(member, bytes) else member)

        for daemon_id in daemon_ids:
            hash_key = DAEMON_HASH.format(daemon_id=daemon_id)
            exists = await self._redis.exists(hash_key)
            if exists:
                continue  # alive

            # Heartbeat expired — dead daemon.
            logger.warning("[DaemonMonitor] daemon=%s DEAD (heartbeat expired)",
                           daemon_id)
            await self._recover_inbox(daemon_id)
            # Clean up.
            await self._redis.srem(DAEMONS_SET, daemon_id)
            await self._redis.delete(hash_key)

    async def _recover_inbox(self, daemon_id: str) -> None:
        """Recover unacked messages from a dead daemon's inbox."""
        inbox_key = INBOX_KEY.format(daemon_id=daemon_id)

        try:
            pending = await self._redis.xpending(inbox_key, DAEMON_GROUP)
        except Exception:
            # Stream or group doesn't exist — nothing to recover.
            return

        if not pending:
            return

        # xpending returns either a dict {"pending": N, ...} or a tuple/list
        # depending on redis-py version.
        pending_count: int
        if isinstance(pending, dict):
            pending_count = int(pending.get("pending", 0))
        elif isinstance(pending, (list, tuple)):
            pending_count = int(pending[0]) if pending else 0
        else:
            pending_count = 0

        if pending_count == 0:
            return

        # Fetch pending message IDs (simplified API gives 0..N-1, min-max, count).
        # Use the range form to get individual IDs.
        try:
            entries = await self._redis.xpending_range(
                inbox_key, DAEMON_GROUP, min="-", max="+", count=pending_count,
            )
        except AttributeError:
            # Older redis-py without xpending_range — fall back to
            # low-level xpending with consumer filter.
            # xpending(name, group, min, max, count) returns list of dicts.
            entries = await self._redis.xpending(
                inbox_key, DAEMON_GROUP, "-", "+", pending_count,
            )

        recovered = 0
        for entry in entries:
            msg_id = entry["message_id"] if isinstance(entry, dict) else entry[0]
            # Read the message body.
            msgs = await self._redis.xrange(inbox_key, min=msg_id, max=msg_id)
            if not msgs:
                continue
            _, fields_raw = msgs[0]
            # Decode bytes→str.
            fields: dict[str, str] = {}
            for k, v in fields_raw.items():
                key = k.decode() if isinstance(k, bytes) else k
                val = v.decode() if isinstance(v, bytes) else v
                fields[key] = val

            logger.info("[DaemonMonitor] recovering bug_id=%s from dead daemon=%s",
                        fields.get("bug_id"), daemon_id)

            # Re-dispatch.
            await self._dispatcher.spawn_raw(fields)

            # XACK + XDEL from the dead daemon's inbox.
            try:
                await self._redis.xack(inbox_key, DAEMON_GROUP, msg_id)
            except Exception:
                pass
            recovered += 1

        if recovered:
            logger.warning(
                "[DaemonMonitor] recovered %d tasks from dead daemon=%s",
                recovered, daemon_id,
            )

        # Clean up the inbox stream itself.
        try:
            await self._redis.delete(inbox_key)
        except Exception:
            pass

    # ── pending / rejected retry ─────────────────────────────────

    async def _retry_pending(self) -> None:
        """Retry tasks from the pending queue."""
        await self._retry_stream(PENDING_STREAM, "pending")

    async def _retry_rejected(self) -> None:
        """Retry tasks that were rejected by daemons."""
        await self._retry_stream(REJECTED_STREAM, "rejected")

    async def _retry_stream(self, stream_key: str, label: str) -> None:
        """
        Read from a recovery stream and re-dispatch each task.
        Ensures the consumer group exists first.
        """
        # Ensure group exists.
        try:
            await self._redis.xgroup_create(
                stream_key, ORCH_GROUP, mkstream=True,
            )
        except Exception:
            pass

        # Read pending first (crashed mid-processing), then new.
        try:
            pending = await self._redis.xpending(stream_key, ORCH_GROUP)
            if isinstance(pending, dict):
                pending_count = int(pending.get("pending", 0))
            elif isinstance(pending, (list, tuple)):
                pending_count = int(pending[0]) if pending else 0
            else:
                pending_count = 0
        except Exception:
            pending_count = 0

        if pending_count > 0:
            await self._claim_and_retry(stream_key, label)

        # Read new messages.
        try:
            msgs = await self._redis.xreadgroup(
                groupname=ORCH_GROUP,
                consumername="orchestrator",
                streams={stream_key: ">"},
                count=10,
                block=1000,
            )
        except Exception:
            return

        if not msgs:
            return

        for stream_name, entries in msgs:
            for msg_id, fields_raw in entries:
                fields: dict[str, str] = {}
                for k, v in fields_raw.items():
                    key = k.decode() if isinstance(k, bytes) else k
                    val = v.decode() if isinstance(v, bytes) else v
                    fields[key] = val

                logger.info("[DaemonMonitor] retrying %s bug_id=%s",
                            label, fields.get("bug_id"))
                await self._dispatcher.spawn_raw(fields)
                await self._redis.xack(stream_key, ORCH_GROUP, msg_id)

    async def _claim_and_retry(self, stream_key: str, label: str) -> None:
        """
        Claim pending messages from the recovery stream (left over from a
        previous orchestrator crash) and re-dispatch them.
        """
        try:
            claimed = await self._redis.xautoclaim(
                stream_key, ORCH_GROUP, "orchestrator",
                min_idle_time=30_000,  # 30 s idle → claim
                count=10,
            )
        except Exception:
            return

        # xautoclaim returns (next_id, [messages]) where messages are
        # (msg_id, fields_dict) tuples.
        if not claimed:
            return

        # redis-py >= 4.5.0 returns (next_id, [messages])
        # Each message is (msg_id, {field: value, ...})
        if len(claimed) >= 2:
            for msg_id, fields_raw in claimed[1]:
                fields: dict[str, str] = {}
                for k, v in fields_raw.items():
                    key = k.decode() if isinstance(k, bytes) else k
                    val = v.decode() if isinstance(v, bytes) else v
                    fields[key] = val

                logger.info("[DaemonMonitor] claimed %s bug_id=%s",
                            label, fields.get("bug_id"))
                await self._dispatcher.spawn_raw(fields)
                await self._redis.xack(stream_key, ORCH_GROUP, msg_id)
