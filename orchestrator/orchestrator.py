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
from orchestrator.spawner import WorkerSpawner, DockerWorkerSpawner, EcsWorkerSpawner, K8sJobSpawner

class Orchestrator:
    def __init__(self, settings=None):
        # Allow external (test) callers to pass custom settings; default to module-level singleton
        self._cfg = settings or cfg
        logger.debug(f"{self._cfg=}")
        self._redis = aioredis.from_url(self._cfg.redis_url, decode_responses=False)
        self._registry = WorkerRegistry()
        # Spawner selection is decoupled from the GitLab env via WORKER_SPAWNER.
        # Empty/"auto" reproduces the historical by-env mapping exactly, so
        # every existing env + test is byte-identical.
        spawner_kind = (getattr(self._cfg, "worker_spawner", "") or "").lower()
        if spawner_kind in ("", "auto"):
            if self._cfg.env in ("local_docker_compose", "local_docker_compose_http"):
                spawner_kind = "docker"
            elif self._cfg.env == "local_k8s":
                spawner_kind = "k8s"
            else:
                spawner_kind = "process"

        if spawner_kind == "docker":
            from pathlib import Path
            worker_env_file = str(Path(__file__).resolve().parent.parent / "settings" / f"worker_{self._cfg.env}.env")
            self._spawner = DockerWorkerSpawner(
                registry=self._registry,
                redis_url=self._cfg.redis_url,
                worker_image=self._cfg.worker_image,
                docker_network=self._cfg.docker_network,
                ssh_private_key=self._cfg.ssh_private_key,
                worker_env_file=worker_env_file,
                env=self._cfg.env,
            )
        elif spawner_kind == "k8s":
            self._spawner = K8sJobSpawner(
                registry=self._registry,
                redis_url=self._cfg.redis_url,
                worker_image=self._cfg.worker_image,
                namespace=self._cfg.k8s_namespace,
                worker_config_map=self._cfg.k8s_worker_config_map,
                secret_name=self._cfg.k8s_secret_name,
                job_ttl_seconds=self._cfg.k8s_job_ttl_seconds,
            )
        elif spawner_kind == "ecs":
            self._spawner = EcsWorkerSpawner(
                registry=self._registry,
                # Empty `ecs_worker_redis_url` falls back to the orchestrator's
                # own redis_url. With worker_network_mode=host the worker
                # shares the host's network namespace so localhost works;
                # under awsvpc set this to the EC2 host's private IPv4.
                redis_url=(self._cfg.ecs_worker_redis_url or self._cfg.redis_url),
                cluster=self._cfg.ecs_cluster_name,
                task_def=self._cfg.ecs_worker_task_def,
                subnets=self._cfg.ecs_worker_subnets,
                security_groups=self._cfg.ecs_worker_security_groups,
                region=self._cfg.ecs_region,
                worker_env=self._cfg.ecs_worker_env,
                worker_network_mode=self._cfg.ecs_worker_network_mode,
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
            completed_key_tpl=self._cfg.worker_completed_key,
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
