import asyncio
import logging
import time

from redis.asyncio import Redis

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
    ):
        self._registry = registry
        self._spawner = spawner
        self._redis = redis
        self._heartbeat_key_tpl = heartbeat_key_tpl
        self._check_interval = check_interval
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
                logger.warning(f"[Monitor] bug_id={bug_id} heartbeat expired (ttl={ttl}), restarting")
                await self._restart(bug_id)

    async def _restart(self, bug_id: str) -> None:
        entry = self._registry.get(bug_id)
        try:
            await self._spawner.restart(bug_id, entry.project_id, entry.project_web_url, entry.job_id)
        except Exception as e:
            logger.error(f"[Monitor] restart failed bug_id={bug_id}: {e}")
            self._registry.update_status(bug_id, "failed")
