import asyncio
import logging
import time

from redis.asyncio import Redis

from .metrics import ACTIVE_WORKERS, STREAM_PENDING, WORKER_RESTARTS
from .registry import WorkerRegistry
from .spawner import WorkerSpawner

logger = logging.getLogger(__name__)


class HealthMonitor:
    def __init__(
        self,
        registry: WorkerRegistry,
        spawner: WorkerSpawner,
        redis: Redis,
        heartbeat_key_tpl: str,
        check_interval: int = 5,
        completed_key_tpl: str = "worker:completed:{bug_id}",
        gateway_stream: str = "",
        gateway_consumer_group: str = "",
        done_grace_seconds: float = 60.0,
    ):
        self._registry = registry
        self._spawner = spawner
        self._redis = redis
        self._heartbeat_key_tpl = heartbeat_key_tpl
        self._completed_key_tpl = completed_key_tpl
        self._check_interval = check_interval
        # Stream/group identity for the stream_pending gauge. Empty
        # strings disable that sampling (e.g. tests that don't wire
        # Redis streams). Sampled here rather than as a separate task
        # because HealthMonitor already runs every check_interval and
        # the XPENDING call is cheap.
        self._gateway_stream = gateway_stream
        self._gateway_consumer_group = gateway_consumer_group
        # Grace period before a terminal registry entry is swept out.
        # See WorkerRegistry.sweep_stale docstring.
        self._done_grace_seconds = done_grace_seconds
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.get_event_loop().create_task(
            self._run(), name="HealthMonitor"
        )
        logger.info("[Monitor] started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[Monitor] stopped")

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._check_interval)
            await self._check_all()

    async def _check_all(self) -> None:
        now = time.time()
        # Sweep terminal entries before refreshing gauges so the
        # capacity numbers reflect the post-sweep registry state.
        # Cheap O(n) walk of the registry dict; n stays small because
        # of the sweep.
        self._registry.sweep_stale(self._done_grace_seconds)
        # Refresh capacity / backlog gauges every tick. This is the
        # primary "is the system healthy now" signal for the dashboard.
        self._refresh_gauges()
        for bug_id, entry in list(self._registry.all_active().items()):
            hb_key = self._heartbeat_key_tpl.format(bug_id=bug_id)
            ttl = await self._redis.ttl(hb_key)

            # For DockerProcessProxy, refresh container state before checking
            if hasattr(entry.process, 'reload_status'):
                entry.process.reload_status()

            if entry.process.returncode is not None:
                logger.info(f"[Monitor] bug_id={bug_id} process exited (rc={entry.process.returncode}), marking done")
                self._registry.update_status(bug_id, "done")
                continue

            if ttl > 0:
                if entry.status == "warmup":
                    logger.info(f"[Monitor] bug_id={bug_id} first heartbeat detected, warmup -> running")
                    self._registry.update_status(bug_id, "running")
                else:
                    logger.info(f"[Monitor] bug_id={bug_id} healthy ttl={ttl}s")
            elif entry.status == "warmup":
                if now < entry.warmup_deadline:
                    logger.debug(f"[Monitor] bug_id={bug_id} warmup: no heartbeat yet, waiting")
                else:
                    logger.warning(f"[Monitor] bug_id={bug_id} warmup timed out, marking failed")
                    self._registry.update_status(bug_id, "failed")
            else:
                # Heartbeat expired AND the spawner-specific process status
                # hasn't reported the exit code yet. Distinguish "worker
                # exited cleanly but the container runtime is slow to report
                # STOPPED" from "worker actually crashed" by checking the
                # completion key the worker SETs in its `finally`. Without
                # this check, a successful ECS run misfires ~30-60 restarts
                # in the window between worker exit and lastStatus=STOPPED
                # (verified on the 2026-05-21 MR !6 run).
                completed_key = self._completed_key_tpl.format(bug_id=bug_id)
                if await self._redis.exists(completed_key):
                    logger.info(
                        f"[Monitor] bug_id={bug_id} heartbeat expired but completion "
                        f"key {completed_key} present — marking done (worker exited cleanly)"
                    )
                    self._registry.update_status(bug_id, "done")
                    continue
                logger.warning(f"[Monitor] bug_id={bug_id} heartbeat expired (ttl={ttl}), restarting")
                WORKER_RESTARTS.labels(cause="heartbeat_expired").inc()
                await self._restart(bug_id)

    async def _restart(self, bug_id: str) -> None:
        entry = self._registry.get(bug_id)
        try:
            await self._spawner.restart(
                bug_id, entry.project_id, entry.project_web_url, entry.job_id,
                # getattr fallback keeps the existing fake-entry test fixtures
                # working (SimpleNamespace without source_branch) — real
                # WorkerEntry instances always carry the field.
                source_branch=getattr(entry, "source_branch", "") or "",
            )
        except Exception as e:
            logger.error(f"[Monitor] restart failed bug_id={bug_id}: {e}")
            self._registry.update_status(bug_id, "failed")

    def _refresh_gauges(self) -> None:
        """Update gauges from the in-memory registry (cheap) and from
        Redis XPENDING (one async call). Run every check_interval — no
        need for a dedicated metrics task. Both gauges are safe to
        leave stale if a single tick errors out; the next tick rewrites
        them.

        Only counts statuses registry.all_active() returns
        (warmup + running). Exposing done/failed on this gauge would be
        misleading because all_active() filters them out anyway, AND
        done/failed entries are not currently removed from the registry
        (separate cleanup gap — see comment in registry.py), so a
        `done` label would look like an unbounded leak.
        """
        counts: dict[str, int] = {"warmup": 0, "running": 0}
        for entry in self._registry.all_active().values():
            counts[entry.status] = counts.get(entry.status, 0) + 1
        for status, n in counts.items():
            ACTIVE_WORKERS.labels(status=status).set(n)
        # Stream pending sampling. Fire-and-forget — failure on Redis-
        # side is already tracked by the consumer's own retry loop.
        if self._gateway_stream and self._gateway_consumer_group:
            asyncio.create_task(self._sample_stream_pending())

    async def _sample_stream_pending(self) -> None:
        try:
            res = await self._redis.xpending(
                self._gateway_stream, self._gateway_consumer_group
            )
            # redis-py returns either a dict ({"pending": N, …}) or a
            # 4-tuple (count, …) depending on version — handle both.
            if isinstance(res, dict):
                pending = int(res.get("pending", 0))
            elif isinstance(res, (list, tuple)) and res:
                pending = int(res[0])
            else:
                pending = 0
        except Exception:
            return
        STREAM_PENDING.labels(
            stream=self._gateway_stream,
            group=self._gateway_consumer_group,
        ).set(pending)
