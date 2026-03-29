import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from .models import WorkerEntry, WARMUP_GRACE
from .registry import WorkerRegistry

logger = logging.getLogger(__name__)

WORKER_SCRIPT = str(Path(__file__).parent.parent / "bf_worker/bf_worker.py")


class WorkerSpawner:
    """Spawns workers as local subprocesses (local_multi_process and other non-docker modes)."""

    def __init__(self, registry: WorkerRegistry, redis_url: str):
        self._registry = registry
        self._redis_url = redis_url

    async def spawn(self, bug_id: str, project_id: str, project_web_url: str, job_id: str) -> WorkerEntry:
        if self._registry.exists(bug_id):
            logger.warning("[Spawner] bug_id=%s already running, skip", bug_id)
            return self._registry.get(bug_id)

        entry = await self._start_process(bug_id, project_id, project_web_url, job_id)
        self._registry.register(entry)
        return entry

    async def restart(self, bug_id: str, project_id: str, project_web_url: str, job_id: str) -> WorkerEntry:
        old = self._registry.get(bug_id)
        restart_count = (old.restart_count + 1) if old else 1

        if old and old.process:
            try:
                old.process.terminate()
                await asyncio.wait_for(old.process.wait(), timeout=3)
            except Exception as e:
                logger.warning("[Spawner] terminate bug_id=%s: %s", bug_id, e)
                try:
                    old.process.kill()
                except Exception:
                    pass

        entry = await self._start_process(bug_id, project_id, project_web_url, job_id, restart_count=restart_count)
        self._registry.register(entry)
        logger.info("[Spawner] restarted bug_id=%s restart_count=%d", bug_id, restart_count)
        return entry

    async def _start_process(self, bug_id: str, project_id: str, project_web_url: str, job_id: str    , restart_count: int = 0) -> WorkerEntry:
        env = os.environ.copy()
        env["REDIS_URL"] = self._redis_url
        env["BUG_ID"] = bug_id
        env["project_id"] = project_id
        env["project_web_url"] = project_web_url
        env["job_id"] = job_id
        process = await asyncio.create_subprocess_exec(
            sys.executable, WORKER_SCRIPT, "--bug-id", bug_id,
            env=env,
        )
        logger.info("[Spawner] started bug_id=%s pid=%s", bug_id, process.pid)
        now = time.time()
        return WorkerEntry(
            bug_id=bug_id,
            process=process,
            project_id=project_id,
            project_web_url=project_web_url,
            job_id=job_id,
            started_at=now,
            warmup_deadline=now + WARMUP_GRACE,
            restart_count=restart_count,
        )


# ── Docker mode ──────────────────────────────────────────────────

class DockerProcessProxy:
    """
    Wraps a Docker container to provide an interface compatible with
    asyncio.subprocess.Process (pid, returncode, terminate, kill, wait).
    """

    def __init__(self, container):
        self._container = container
        self._returncode = None

    @property
    def pid(self):
        """Return the container short ID as a pseudo-pid."""
        return self._container.short_id

    @property
    def returncode(self):
        return self._returncode

    def terminate(self):
        try:
            self._container.stop(timeout=5)
        except Exception:
            pass

    def kill(self):
        try:
            self._container.kill()
        except Exception:
            pass

    async def wait(self):
        """Poll container status until it exits."""
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self._container.wait)
        self._returncode = result.get("StatusCode", -1)
        return self._returncode

    def reload_status(self):
        """Refresh container state and update returncode if exited."""
        try:
            self._container.reload()
            if self._container.status == "exited":
                exit_code = self._container.attrs["State"].get("ExitCode", -1)
                self._returncode = exit_code
        except Exception:
            self._returncode = -1


class DockerWorkerSpawner:
    """Spawns workers as Docker containers (local_docker_compose mode)."""

    def __init__(self, registry: WorkerRegistry, redis_url: str,
                 worker_image: str, docker_network: str, ssh_private_key: str,
                 worker_env_file: str):
        self._registry = registry
        self._redis_url = redis_url
        self._worker_image = worker_image
        self._docker_network = docker_network
        self._ssh_private_key = ssh_private_key
        self._worker_env_file = worker_env_file

        import docker
        self._docker = docker.from_env()

    async def spawn(self, bug_id: str, project_id: str, project_web_url: str, job_id: str) -> WorkerEntry:
        if self._registry.exists(bug_id):
            logger.warning("[DockerSpawner] bug_id=%s already running, skip", bug_id)
            return self._registry.get(bug_id)

        entry = await self._start_container(bug_id, project_id, project_web_url, job_id)
        self._registry.register(entry)
        return entry

    async def restart(self, bug_id: str, project_id: str, project_web_url: str, job_id: str) -> WorkerEntry:
        old = self._registry.get(bug_id)
        restart_count = (old.restart_count + 1) if old else 1

        if old and old.process:
            try:
                old.process.terminate()
                await asyncio.wait_for(old.process.wait(), timeout=5)
            except Exception as e:
                logger.warning("[DockerSpawner] terminate bug_id=%s: %s", bug_id, e)
                try:
                    old.process.kill()
                except Exception:
                    pass

        entry = await self._start_container(bug_id, project_id, project_web_url, job_id, restart_count=restart_count)
        self._registry.register(entry)
        logger.info("[DockerSpawner] restarted bug_id=%s restart_count=%d", bug_id, restart_count)
        return entry

    async def _start_container(self, bug_id: str, project_id: str, project_web_url: str, job_id: str,
                               restart_count: int = 0) -> WorkerEntry:
        environment = {
            "BUG_ID": bug_id,
            "REDIS_URL": self._redis_url,
            "project_id": project_id,
            "project_web_url": project_web_url,
            "job_id": job_id,
            "ENV": "local_docker_compose",
        }

        if self._ssh_private_key:
            environment["SSH_PRIVATE_KEY"] = self._ssh_private_key

        loop = asyncio.get_event_loop()
        container = await loop.run_in_executor(
            None,
            lambda: self._docker.containers.run(
                self._worker_image,
                command=["--bug-id", bug_id],
                environment=environment,
                network=self._docker_network,
                name=f"dh-bf-worker-{bug_id}",
                detach=True,
            ),
        )

        proxy = DockerProcessProxy(container)
        logger.info("[DockerSpawner] started bug_id=%s container=%s", bug_id, container.short_id)

        now = time.time()
        return WorkerEntry(
            bug_id=bug_id,
            process=proxy,
            project_id=project_id,
            project_web_url=project_web_url,
            job_id=job_id,
            started_at=now,
            warmup_deadline=now + WARMUP_GRACE,
            restart_count=restart_count,
        )
