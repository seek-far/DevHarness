import asyncio
import logging
import os
import re
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
        if os.getenv("BF_AGENT_CONFIG"):
            env["BF_AGENT_CONFIG"] = os.environ["BF_AGENT_CONFIG"]
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
                 worker_env_file: str, env: str = "local_docker_compose"):
        self._registry = registry
        self._redis_url = redis_url
        self._worker_image = worker_image
        self._docker_network = docker_network
        self._ssh_private_key = ssh_private_key
        self._worker_env_file = worker_env_file
        # ENV the spawned worker container runs under. Default keeps the
        # existing local_docker_compose caller byte-identical;
        # local_docker_compose_http passes its own env through.
        self._env = env

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
            "ENV": self._env,
            # Per-bug worker containers are ephemeral — a persistent sqlite
            # checkpoint has no resume value and re-creates the shared-state
            # contamination hazard (see project memory). Disabling also avoids
            # depending on the optional langgraph-checkpoint-sqlite package in
            # the worker image. Checkpointing is a pure perf optimization;
            # "none" == the pre-checkpointing behaviour.
            "BF_CHECKPOINT_BACKEND": "none",
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


# ── k8s Job mode (local_k8s) ─────────────────────────────────────

def _k8s_job_name(bug_id: str, restart_count: int = 0) -> str:
    """
    Map a bug_id to a DNS-1123-label-safe Job name.

    bug_ids look like ``2026_05_15-12_30_45_3`` — underscores and length are
    both illegal for k8s object names. We lowercase, replace every non-alnum
    run with a single ``-``, prefix ``bf-worker-`` and cap at 63 chars. The
    restart_count suffix keeps orchestrator-driven restarts from colliding
    with the (possibly still-terminating) previous Job of the same bug.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", bug_id.lower()).strip("-")
    name = f"bf-worker-{slug}"
    if restart_count:
        name = f"{name}-r{restart_count}"
    return name[:63].rstrip("-")


class K8sJobProxy:
    """
    Wraps a k8s Job to provide an interface compatible with
    asyncio.subprocess.Process (pid, returncode, terminate, kill, wait)
    plus reload_status() (HealthMonitor refreshes via it, same as Docker).
    """

    def __init__(self, batch_api, job_name: str, namespace: str):
        self._batch = batch_api
        self._job_name = job_name
        self._namespace = namespace
        self._returncode = None

    @property
    def pid(self):
        """The Job name doubles as a human-readable pseudo-pid."""
        return self._job_name

    @property
    def returncode(self):
        return self._returncode

    def _delete(self) -> None:
        from kubernetes import client
        try:
            self._batch.delete_namespaced_job(
                name=self._job_name,
                namespace=self._namespace,
                body=client.V1DeleteOptions(propagation_policy="Background"),
            )
        except Exception as e:  # already gone / API down — best effort
            logger.warning("[K8sSpawner] delete job=%s: %s", self._job_name, e)

    def terminate(self) -> None:
        self._delete()

    def kill(self) -> None:
        self._delete()

    def reload_status(self) -> None:
        """Refresh Job state; set returncode once the Job is terminal."""
        from kubernetes.client.rest import ApiException
        try:
            status = self._batch.read_namespaced_job_status(
                name=self._job_name, namespace=self._namespace
            ).status
        except ApiException as e:
            if e.status == 404:
                # Job GC'd (TTL) or deleted out from under us → treat as done.
                self._returncode = 0 if self._returncode is None else self._returncode
            else:
                logger.warning("[K8sSpawner] read job=%s status: %s", self._job_name, e)
            return
        if status.succeeded:
            self._returncode = 0
        elif status.failed:
            self._returncode = 1
        # else still active → leave returncode None

    async def wait(self):
        """Poll Job status until it reaches a terminal state."""
        loop = asyncio.get_event_loop()
        while self._returncode is None:
            await loop.run_in_executor(None, self.reload_status)
            if self._returncode is None:
                await asyncio.sleep(2)
        return self._returncode


class K8sJobSpawner:
    """Spawns workers as k8s Jobs (local_k8s mode).

    One Job per bug, image = ``worker_image``, non-secret config from the
    ``worker-config`` ConfigMap + secrets from ``sdlcma-secrets`` (both stood
    up in 1a.2), per-bug values injected as explicit env. ``backoffLimit=0``
    (the orchestrator's HealthMonitor owns restart policy, not the Job
    controller) and ``ttlSecondsAfterFinished`` so finished Jobs self-GC.
    """

    def __init__(self, registry: WorkerRegistry, redis_url: str,
                 worker_image: str, namespace: str,
                 worker_config_map: str, secret_name: str,
                 job_ttl_seconds: int):
        self._registry = registry
        self._redis_url = redis_url
        self._worker_image = worker_image
        self._namespace = namespace
        self._worker_config_map = worker_config_map
        self._secret_name = secret_name
        self._job_ttl_seconds = job_ttl_seconds

        from kubernetes import client, config
        from kubernetes.config.config_exception import ConfigException
        try:
            config.load_incluster_config()  # orchestrator pod's service account
            logger.info("[K8sSpawner] using in-cluster config")
        except ConfigException:
            config.load_kube_config()        # dev: orchestrator run outside k8s
            logger.info("[K8sSpawner] using local kubeconfig")
        self._batch = client.BatchV1Api()

    async def spawn(self, bug_id: str, project_id: str, project_web_url: str, job_id: str) -> WorkerEntry:
        if self._registry.exists(bug_id):
            logger.warning("[K8sSpawner] bug_id=%s already running, skip", bug_id)
            return self._registry.get(bug_id)

        entry = await self._start_job(bug_id, project_id, project_web_url, job_id)
        self._registry.register(entry)
        return entry

    async def restart(self, bug_id: str, project_id: str, project_web_url: str, job_id: str) -> WorkerEntry:
        old = self._registry.get(bug_id)
        restart_count = (old.restart_count + 1) if old else 1

        if old and old.process:
            try:
                old.process.terminate()
            except Exception as e:
                logger.warning("[K8sSpawner] terminate bug_id=%s: %s", bug_id, e)

        entry = await self._start_job(bug_id, project_id, project_web_url, job_id,
                                      restart_count=restart_count)
        self._registry.register(entry)
        logger.info("[K8sSpawner] restarted bug_id=%s restart_count=%d", bug_id, restart_count)
        return entry

    def _build_job(self, job_name: str, bug_id: str, project_id: str,
                   project_web_url: str, job_id: str):
        from kubernetes import client

        env = [
            client.V1EnvVar(name="BUG_ID", value=bug_id),
            client.V1EnvVar(name="project_id", value=project_id),
            client.V1EnvVar(name="project_web_url", value=project_web_url),
            client.V1EnvVar(name="job_id", value=job_id),
            client.V1EnvVar(name="REDIS_URL", value=self._redis_url),
        ]
        if os.getenv("BF_AGENT_CONFIG"):
            env.append(client.V1EnvVar(name="BF_AGENT_CONFIG",
                                       value=os.environ["BF_AGENT_CONFIG"]))

        container = client.V1Container(
            name="bf-worker",
            image=self._worker_image,
            image_pull_policy="IfNotPresent",  # kind-loaded image, never on a registry
            args=["--bug-id", bug_id],         # entrypoint.sh: exec python bf_worker.py "$@"
            env=env,
            env_from=[
                client.V1EnvFromSource(
                    config_map_ref=client.V1ConfigMapEnvSource(name=self._worker_config_map)),
                client.V1EnvFromSource(
                    secret_ref=client.V1SecretEnvSource(name=self._secret_name)),
            ],
        )
        pod_spec = client.V1PodSpec(
            restart_policy="Never",
            # The worker talks only to Redis / GitLab / the LLM — never the
            # k8s API. Don't mount the (default) SA token: removes a useless
            # credential a hijacked worker could otherwise reach for. Defense
            # in depth alongside the patch/fetch/prompt guards.
            automount_service_account_token=False,
            containers=[container],
        )
        template = client.V1PodTemplateSpec(
            metadata=client.V1ObjectMeta(labels={"app": "bf-worker", "bug-id": job_name}),
            spec=pod_spec,
        )
        return client.V1Job(
            metadata=client.V1ObjectMeta(
                name=job_name,
                namespace=self._namespace,
                labels={"app": "bf-worker", "bug-id": job_name},
            ),
            spec=client.V1JobSpec(
                backoff_limit=0,                              # orchestrator owns retries
                ttl_seconds_after_finished=self._job_ttl_seconds,
                template=template,
            ),
        )

    async def _start_job(self, bug_id: str, project_id: str, project_web_url: str,
                          job_id: str, restart_count: int = 0) -> WorkerEntry:
        from kubernetes.client.rest import ApiException

        job_name = _k8s_job_name(bug_id, restart_count)
        job = self._build_job(job_name, bug_id, project_id, project_web_url, job_id)

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: self._batch.create_namespaced_job(
                    namespace=self._namespace, body=job),
            )
        except ApiException as e:
            if e.status == 409:
                # A Job with this name already exists (crash between create and
                # registry.register, or a racing spawn). Adopt it rather than
                # duplicating — same idempotency stance as the GitLab provider.
                logger.warning("[K8sSpawner] job=%s already exists, adopting", job_name)
            else:
                raise

        proxy = K8sJobProxy(self._batch, job_name, self._namespace)
        logger.info("[K8sSpawner] started bug_id=%s job=%s", bug_id, job_name)

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
