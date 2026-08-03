#python -m orchestrator.orchestrator
import asyncio
import logging
import os
import secrets
import sys
import time
from datetime import datetime

import redis.asyncio as aioredis
from prometheus_client import start_http_server

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s [orch %(name)s:%(funcName)s:%(lineno)d] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)
logging.getLogger("docker").setLevel(logging.WARNING)

from settings import orchestrator_cfg as cfg
from orchestrator.consumer import StreamConsumer
from orchestrator.metrics import SPAWN_WALLCLOCK_MS
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
            # k8s_host_aliases is a JSON string in the orchestrator ConfigMap
            # (env-friendly: a single line scalar Pydantic can carry). Empty
            # string → no aliases. getattr fallbacks keep SimpleNamespace
            # test fixtures working without forcing every fake config to
            # declare these new fields.
            import json
            ha_raw = getattr(self._cfg, "k8s_host_aliases", "") or ""
            try:
                host_aliases = json.loads(ha_raw) if ha_raw.strip() else []
            except json.JSONDecodeError as e:
                logger.error("[Orchestrator] k8s_host_aliases is not valid JSON "
                             "(%s); proceeding with empty hostAliases", e)
                host_aliases = []
            self._spawner = K8sJobSpawner(
                registry=self._registry,
                redis_url=self._cfg.redis_url,
                worker_image=self._cfg.worker_image,
                namespace=self._cfg.k8s_namespace,
                worker_config_map=self._cfg.k8s_worker_config_map,
                secret_name=self._cfg.k8s_secret_name,
                job_ttl_seconds=self._cfg.k8s_job_ttl_seconds,
                host_aliases=host_aliases,
                journal_host_path=getattr(self._cfg, "k8s_journal_host_path", "") or "",
                # W4 worker Job shaping. getattr keeps older Settings objects
                # (and the test fixtures built on them) constructible; every
                # field is default-empty, so an unset one leaves the Job spec
                # exactly as it was.
                cpu_request=getattr(self._cfg, "k8s_worker_cpu_request", "") or "",
                mem_request=getattr(self._cfg, "k8s_worker_mem_request", "") or "",
                cpu_limit=getattr(self._cfg, "k8s_worker_cpu_limit", "") or "",
                mem_limit=getattr(self._cfg, "k8s_worker_mem_limit", "") or "",
                ephemeral_storage_request=getattr(
                    self._cfg, "k8s_worker_ephemeral_storage_request", "") or "",
                docker_sock=getattr(self._cfg, "k8s_worker_docker_sock", "") or "",
                step_checkpoint_host_path=getattr(
                    self._cfg, "k8s_worker_step_checkpoint_host_path", "") or "",
                node_selector=getattr(self._cfg, "k8s_worker_node_selector", "") or "",
                tolerations=getattr(self._cfg, "k8s_worker_tolerations", "") or "",
                resume_affinity=getattr(
                    self._cfg, "k8s_worker_resume_affinity", "preferred") or "preferred",
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
        elif spawner_kind == "distr-pull":
            from orchestrator.dispatcher import DistributedDispatcher
            from orchestrator.daemon_monitor import DaemonMonitor
            self._spawner = DistributedDispatcher(
                redis=self._redis,
                registry=self._registry,
                cache_ttl=getattr(self._cfg, "worker_daemon_cache_ttl", 5),
            )
            self._daemon_monitor = DaemonMonitor(
                redis=self._redis,
                dispatcher=self._spawner,
                interval=getattr(self._cfg, "worker_daemon_recovery_interval", 15),
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
            # Optional: when both are non-empty, the monitor refreshes
            # sdlcma_stream_pending every check_interval. Empty strings
            # disable that sampling (e.g. integration tests).
            gateway_stream=self._cfg.gateway_stream,
            gateway_consumer_group=self._cfg.gateway_consumer_group,
            # getattr fallback keeps the SimpleNamespace test fixtures
            # working without forcing every fake config to declare the
            # new field. Real OrchestratorSettings always has it.
            done_grace_seconds=getattr(
                self._cfg, "worker_registry_done_grace_seconds", 60.0
            ),
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
            source_branch = event.source_branch or ""
            now = datetime.now()
            # The deciseconds component (`now.microsecond // 100000`) gives
            # 100 ms resolution — concurrent webhooks within that window
            # collide (verified at N=8 burst; memory project_orchestrator_
            # bug_id_race). Append a 4-hex-char tail drawn from
            # `secrets.token_hex` (urandom-backed, not time-based, so it
            # can't collide with the timestamp's own derivation) to make
            # bug_id unique-per-webhook with overwhelming probability
            # (collision odds 1/65536 per same-decisecond webhook pair).
            bug_id = (
                now.strftime("%Y_%m_%d-%H_%M_%S")
                + f"_{now.microsecond // 100000}"
                + f"_{secrets.token_hex(2)}"
            )
            logger.info("[Orchestrator] generate bug_id=%s source_branch=%s", bug_id, source_branch)
            # Phase-1 end / phase-2 start marker. job_id + ref join this
            # line to the gateway's `phase=gateway_received` line; bug_id
            # joins this line to every downstream worker marker. Same
            # `phase_marker` prefix everywhere so one grep rebuilds the
            # whole per-bug timeline across the three services.
            logger.info(
                "phase_marker phase=spawn_start bug_id=%s job_id=%s ref=%s t_wall_ms=%d",
                bug_id, job_id, source_branch, time.time_ns() // 1_000_000,
            )
            _spawn_t0 = time.perf_counter()
            await self._spawner.spawn(bug_id, project_id, project_web_url, job_id, source_branch=source_branch)
            SPAWN_WALLCLOCK_MS.observe((time.perf_counter() - _spawn_t0) * 1000)

        elif isinstance(event, ValidationStatusEvent):
            logger.info(
                "[Orchestrator] validation bug_id=%s status=%s",
                event.bug_id, event.status,
            )
            await self._router.route(event)

    async def run(self) -> None:
        logger.info("[Orchestrator] starting env=%s", self._cfg.env)
        # Prometheus scrape endpoint on a dedicated port. Bind 0.0.0.0 so
        # a scraper in another pod / host can reach it; if you don't want
        # that, set METRICS_BIND=127.0.0.1 (or set METRICS_PORT=0 to
        # disable entirely — useful for unit tests and standalone dev).
        metrics_port = int(os.environ.get("METRICS_PORT", "9102"))
        metrics_bind = os.environ.get("METRICS_BIND", "0.0.0.0")
        if metrics_port > 0:
            try:
                start_http_server(metrics_port, addr=metrics_bind)
                logger.info("[Orchestrator] metrics on http://%s:%d/metrics",
                            metrics_bind, metrics_port)
            except OSError as e:
                # Port collision / permission error — log loudly but don't
                # block the orchestrator from starting. Metrics off is
                # better than no orchestrator.
                logger.error("[Orchestrator] metrics server failed to bind "
                             "%s:%d: %s (metrics disabled this run)",
                             metrics_bind, metrics_port, e)
        self._monitor.start()
        if hasattr(self, "_daemon_monitor"):
            self._daemon_monitor.start()
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
