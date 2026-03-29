#python -m orchestrator.orchestrator
import asyncio
import logging
import sys
from datetime import datetime

import redis.asyncio as aioredis

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s [orch %(name)s:%(funcName)s:%(lineno)d] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)
logging.getLogger("docker").setLevel(logging.WARNING)

from settings import orchestrator_cfg as cfg
from orchestrator.consumer import StreamConsumer
from orchestrator.models import BugReportedEvent, ValidationStatusEvent
from orchestrator.monitor import HealthMonitor
from orchestrator.parser import ParseError, parse_message
from orchestrator.registry import WorkerRegistry
from orchestrator.router import MessageRouter
from orchestrator.spawner import WorkerSpawner, DockerWorkerSpawner

class Orchestrator:
    def __init__(self, settings=None):
        # Allow external (test) callers to pass custom settings; default to module-level singleton
        self._cfg = settings or cfg
        logger.debug(f"{self._cfg=}")
        self._redis = aioredis.from_url(self._cfg.redis_url, decode_responses=False)
        self._registry = WorkerRegistry()
        if self._cfg.env == "local_docker_compose":
            from pathlib import Path
            worker_env_file = str(Path(__file__).resolve().parent.parent / "settings" / f"worker_{self._cfg.env}.env")
            self._spawner = DockerWorkerSpawner(
                registry=self._registry,
                redis_url=self._cfg.redis_url,
                worker_image=self._cfg.worker_image,
                docker_network=self._cfg.docker_network,
                ssh_private_key=self._cfg.ssh_private_key,
                worker_env_file=worker_env_file,
            )
        else:
            self._spawner = WorkerSpawner(self._registry, self._cfg.redis_url)
        self._router = MessageRouter(
            self._registry, self._redis, self._cfg.worker_inbox_stream_key
        )
        self._monitor = HealthMonitor(
            registry=self._registry,
            spawner=self._spawner,
            redis=self._redis,
            heartbeat_key_tpl=self._cfg.worker_heartbeat_key,
            check_interval=self._cfg.health_check_interval,
        )
        self._consumer = StreamConsumer(
            redis=self._redis,
            stream_key=self._cfg.gateway_stream,
            group=self._cfg.gateway_consumer_group,
            consumer_name=self._cfg.gateway_consumer_name,
            handler=self._handle_message,
            dead_letter_stream=self._cfg.dead_letter_stream,
            block_ms=self._cfg.stream_block_ms,
            count=self._cfg.stream_count,
        )

    async def _handle_message(self, raw: bytes) -> None:
        try:
            event = parse_message(raw)
        except ParseError as e:
            logger.error("[Orchestrator] parse error: %s", e)
            raise

        if isinstance(event, BugReportedEvent):
            project_id = str(event.project_id)
            project_web_url = event.project_web_url
            job_id = str(event.job_id)
            now = datetime.now()
            bug_id = now.strftime("%Y_%m_%d-%H_%M_%S") + f"_{now.microsecond // 100000}"
            logger.info("[Orchestrator] generate bug_id=%s", bug_id)
            await self._spawner.spawn(bug_id, project_id, project_web_url, job_id)

        elif isinstance(event, ValidationStatusEvent):
            logger.info(
                "[Orchestrator] validation bug_id=%s status=%s",
                event.bug_id, event.status,
            )
            await self._router.route(event)

    async def run(self) -> None:
        logger.info("[Orchestrator] starting env=%s", self._cfg.env)
        self._monitor.start()
        self._consumer.start()
        logger.info("[Orchestrator] running")

        loop = asyncio.get_running_loop()
        stop = loop.create_future()

        for sig in (2, 15):  # SIGINT, SIGTERM
            try:
                loop.add_signal_handler(sig, stop.set_result, None)
            except NotImplementedError:
                pass  # Windows

        await stop
        logger.info("[Orchestrator] shutting down")
        await self._consumer.stop()
        await self._monitor.stop()
        await self._redis.aclose()
        logger.info("[Orchestrator] stopped")


if __name__ == "__main__":
    asyncio.run(Orchestrator().run())
