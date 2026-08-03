import asyncio
import logging
import os
import re
import sys
import time
from pathlib import Path

from .metrics import BUG_ID_COLLISIONS, WORKERS_SPAWNED
from .models import WorkerEntry, WARMUP_GRACE
from .registry import WorkerRegistry

logger = logging.getLogger(__name__)

WORKER_SCRIPT = str(Path(__file__).parent.parent / "bf_worker/bf_worker.py")


class WorkerSpawner:
    """Spawns workers as local subprocesses (local_multi_process and other non-docker modes)."""

    def __init__(self, registry: WorkerRegistry, redis_url: str):
        self._registry = registry
        self._redis_url = redis_url

    async def spawn(self, bug_id: str, project_id: str, project_web_url: str, job_id: str,
                    source_branch: str = "") -> WorkerEntry:
        if self._registry.exists(bug_id):
            logger.warning("[Spawner] bug_id=%s already running, skip", bug_id)
            BUG_ID_COLLISIONS.inc()
            return self._registry.get(bug_id)

        entry = await self._start_process(bug_id, project_id, project_web_url, job_id,
                                          source_branch=source_branch)
        self._registry.register(entry)
        WORKERS_SPAWNED.labels(spawner="process").inc()
        return entry

    async def restart(self, bug_id: str, project_id: str, project_web_url: str, job_id: str,
                      source_branch: str = "") -> WorkerEntry:
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

        entry = await self._start_process(bug_id, project_id, project_web_url, job_id,
                                          source_branch=source_branch,
                                          restart_count=restart_count)
        self._registry.register(entry)
        logger.info("[Spawner] restarted bug_id=%s restart_count=%d", bug_id, restart_count)
        return entry

    async def _start_process(self, bug_id: str, project_id: str, project_web_url: str, job_id: str,
                             source_branch: str = "", restart_count: int = 0) -> WorkerEntry:
        env = os.environ.copy()
        env["REDIS_URL"] = self._redis_url
        env["BUG_ID"] = bug_id
        env["project_id"] = project_id
        env["project_web_url"] = project_web_url
        env["job_id"] = job_id
        # Empty string keeps the historical default ("main") in the worker.
        env["BUG_SOURCE_BRANCH"] = source_branch or ""
        if os.getenv("BF_AGENT_CONFIG"):
            env["BF_AGENT_CONFIG"] = os.environ["BF_AGENT_CONFIG"]
        process = await asyncio.create_subprocess_exec(
            sys.executable, WORKER_SCRIPT, "--bug-id", bug_id,
            env=env,
        )
        logger.info("[Spawner] started bug_id=%s pid=%s source_branch=%s",
                    bug_id, process.pid, source_branch or "<main-default>")
        now = time.time()
        return WorkerEntry(
            bug_id=bug_id,
            process=process,
            project_id=project_id,
            project_web_url=project_web_url,
            job_id=job_id,
            source_branch=source_branch,
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

    async def spawn(self, bug_id: str, project_id: str, project_web_url: str, job_id: str,
                    source_branch: str = "") -> WorkerEntry:
        if self._registry.exists(bug_id):
            logger.warning("[DockerSpawner] bug_id=%s already running, skip", bug_id)
            BUG_ID_COLLISIONS.inc()
            return self._registry.get(bug_id)

        entry = await self._start_container(bug_id, project_id, project_web_url, job_id,
                                            source_branch=source_branch)
        self._registry.register(entry)
        WORKERS_SPAWNED.labels(spawner="docker").inc()
        return entry

    async def restart(self, bug_id: str, project_id: str, project_web_url: str, job_id: str,
                      source_branch: str = "") -> WorkerEntry:
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

        entry = await self._start_container(bug_id, project_id, project_web_url, job_id,
                                            source_branch=source_branch,
                                            restart_count=restart_count)
        self._registry.register(entry)
        logger.info("[DockerSpawner] restarted bug_id=%s restart_count=%d", bug_id, restart_count)
        return entry

    async def _start_container(self, bug_id: str, project_id: str, project_web_url: str, job_id: str,
                               source_branch: str = "", restart_count: int = 0) -> WorkerEntry:
        environment = {
            "BUG_ID": bug_id,
            "REDIS_URL": self._redis_url,
            "project_id": project_id,
            "project_web_url": project_web_url,
            "job_id": job_id,
            # Empty string keeps the historical default ("main") in the worker.
            "BUG_SOURCE_BRANCH": source_branch or "",
            "ENV": self._env,
            # Per-bug worker containers are ephemeral — a persistent sqlite
            # checkpoint has no resume value and re-creates the shared-state
            # contamination hazard (see project memory). Disabling also avoids
            # depending on the optional langgraph-checkpoint-sqlite package in
            # the worker image. Checkpointing is a pure perf optimization;
            # "none" == the pre-checkpointing behaviour.
            "BF_CHECKPOINT_BACKEND": "none",
        }

        # Worker-side knobs that must survive the spawn boundary. Conditional so
        # an unset variable leaves the container spec byte-identical to before
        # (same "empty means unchanged" convention as journal_host_path).
        # MINI_IMPL / BF_STEP_CHECKPOINT select the vendored mini loop and its
        # intra-loop resume (plan item W2).
        for var in ("MINI_IMPL", "BF_STEP_CHECKPOINT", "BF_AGENT_CONFIG"):
            if os.getenv(var):
                environment[var] = os.environ[var]

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
            source_branch=source_branch,
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


# A label VALUE is a laxer alphabet than a DNS-1123 label (object name):
# underscores and dots are legal, case is preserved, only the first/last
# character must be alphanumeric. Verified against a live 1.36 apiserver that
# real bug_ids go in verbatim — `2026_08_03-11_22_33_4_ab12`,
# `astropy__astropy-12907`, `BUG-LOCAL-1` were all accepted (W4 design, L0).
_LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$")


def _k8s_label_value(value: str) -> str:
    """Make `value` usable as a label value, changing it as little as possible.

    Deliberately NOT `_k8s_job_name`: that one has to satisfy DNS-1123, which
    would mangle every underscore in a bug_id for no reason here.

    The result is for humans and selectors only. The authoritative bug_id is
    the container's BUG_ID env var — never reverse a sanitised label back into
    an identity.
    """
    value = (value or "")[:63]
    if _LABEL_VALUE_RE.match(value):
        return value
    cleaned = re.sub(r"[^-A-Za-z0-9_.]", "-", value).strip("-._")[:63]
    return cleaned or "unknown"


# k8s resource quantity, e.g. "3", "500m", "4Gi", "1.5". Validated at startup
# rather than at spawn time: a typo here makes EVERY create_namespaced_job()
# fail with a 422, i.e. every bug silently dropped, with the errors buried in
# normal log traffic.
_QUANTITY_RE = re.compile(r"^\d+(\.\d+)?(m|k|M|G|T|P|E|Ki|Mi|Gi|Ti|Pi|Ei)?$")

_RESUME_AFFINITY_MODES = ("preferred", "required", "off")


def _parse_json_setting(raw: str, expected: type, what: str):
    """Parse one of the JSON-string settings, tolerating garbage.

    Matches the pre-existing k8s_host_aliases stance: log and continue with
    the empty value. Fatal-on-typo is reserved for the resource quantities,
    where a mistake breaks every spawn rather than one optional feature.
    """
    import json
    if not (raw or "").strip():
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("[K8sSpawner] %s is not valid JSON (%s); ignoring it", what, e)
        return None
    if not isinstance(parsed, expected):
        logger.error("[K8sSpawner] %s must be a JSON %s, got %s; ignoring it",
                     what, expected.__name__, type(parsed).__name__)
        return None
    return parsed or None


_TOLERATION_FIELDS = ("key", "operator", "value", "effect", "toleration_seconds")


def _toleration(raw: dict):
    """Build a V1Toleration from an operator-supplied dict.

    Accepts the K8S WIRE SHAPE (`tolerationSeconds`) as well as the python
    client's snake_case, because everything an operator can copy — the k8s
    docs, `kubectl get -o yaml`, our own Helm values — is camelCase, while
    V1Toleration(**t) only takes snake_case. Without this every spawn would
    die with a TypeError on a config that looks exactly right.

    Unknown keys are dropped with a warning rather than raised: one typo in an
    optional field should not stop every worker from being spawned.
    """
    from kubernetes import client
    kwargs = {}
    for key, value in (raw or {}).items():
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", str(key)).lower()
        if snake in _TOLERATION_FIELDS:
            kwargs[snake] = value
        else:
            logger.warning("[K8sSpawner] toleration field %r is not a k8s "
                           "toleration key — ignoring it", key)
    return client.V1Toleration(**kwargs)


def _worker_resume_enabled() -> bool:
    """Is intra-loop resume (W2/W2.5) on for the workers this orchestrator spawns?

    Read from the orchestrator's OWN environment because that is exactly what
    _build_job forwards to the worker. Gating the restart node affinity on it
    keeps the affinity block out of the Job spec for every deployment that
    isn't resumable — which is what makes "empty config, byte-identical spec"
    hold without a separate switch.
    """
    return (os.getenv("BF_STEP_CHECKPOINT") or "none").strip().lower() != "none"


# ── ECS mode ─────────────────────────────────────────────────────

class EcsTaskProxy:
    """Wraps an ECS task to provide an interface compatible with
    asyncio.subprocess.Process (pid, returncode, terminate, kill, wait)
    plus reload_status() (HealthMonitor refreshes via it, same as Docker/K8s).
    """

    def __init__(self, ecs_client, cluster: str, task_arn: str):
        self._ecs = ecs_client
        self._cluster = cluster
        self._task_arn = task_arn
        self._returncode = None

    @property
    def pid(self):
        return self._task_arn.split("/")[-1]

    @property
    def returncode(self):
        return self._returncode

    def terminate(self):
        try:
            self._ecs.stop_task(cluster=self._cluster, task=self._task_arn)
        except Exception:
            pass

    def kill(self):
        self.terminate()

    def reload_status(self):
        try:
            resp = self._ecs.describe_tasks(
                cluster=self._cluster, tasks=[self._task_arn]
            )
            tasks = resp.get("tasks", [])
            if not tasks:
                # An empty `tasks` list can mean either (a) the freshly-
                # spawned task isn't yet visible to describe-tasks (brief
                # propagation window — ECS RunTask returned an ARN but
                # describe-tasks hasn't caught up), or (b) the task is gone
                # for real. Only the latter has a `failures` entry with
                # reason=="MISSING". For (a) leave returncode=None so the
                # HealthMonitor doesn't mark a live worker as exited 10s
                # after spawn (verified bug; AWS ECS bug-fix run lost the
                # validation-event routing because of this race).
                failures = resp.get("failures", [])
                if any(f.get("reason") == "MISSING" for f in failures):
                    self._returncode = -1
                return
            task = tasks[0]
            if task.get("lastStatus") == "STOPPED":
                containers = task.get("containers", [])
                exit_codes = [
                    c.get("exitCode", -1) for c in containers
                    if c.get("exitCode") is not None
                ]
                self._returncode = exit_codes[0] if exit_codes else -1
        except Exception:
            # Don't kill the worker registry entry just because one
            # describe-tasks call hit a transient error — same rationale as
            # the empty-tasks branch above. Leave returncode=None.
            pass

    async def wait(self):
        loop = asyncio.get_event_loop()
        while self._returncode is None:
            await loop.run_in_executor(None, self.reload_status)
            if self._returncode is None:
                await asyncio.sleep(2)
        return self._returncode


class EcsWorkerSpawner:
    """Spawns workers as one-off ECS tasks (ecs mode).

    The orchestrator itself runs inside ECS and authenticates via its task IAM
    role — no explicit AWS credentials to manage. Each bug spawns one
    ``bf-worker`` task (awsvpc, same VPC as the orchestrator). Fire-and-forget;
    the HealthMonitor polls task status via ``describe_tasks``.
    """

    def __init__(self, registry: WorkerRegistry, redis_url: str,
                 cluster: str, task_def: str, subnets: str,
                 security_groups: str, region: str = "us-east-1",
                 worker_env: str = "gitlab_saas",
                 worker_network_mode: str = "awsvpc"):
        self._registry = registry
        self._redis_url = redis_url
        self._cluster = cluster
        self._task_def = task_def
        self._subnets = [s.strip() for s in subnets.split(",") if s.strip()]
        self._security_groups = [s.strip() for s in security_groups.split(",") if s.strip()]
        self._region = region
        # The ENV the worker container runs under (gitlab.com → gitlab_saas).
        # Matches the established WORKER_SPAWNER-decoupling pattern: spawner is
        # picked separately from the GitLab env. NOT a new "ecs" worker env.
        self._worker_env = worker_env
        # Worker task's network mode — must match the bf-worker task
        # definition's NetworkMode. Used to decide whether to attach
        # networkConfiguration on run_task: ECS REJECTS networkConfiguration
        # for bridge/host modes with InvalidParameterException.
        self._worker_network_mode = worker_network_mode

        import boto3
        self._ecs = boto3.client("ecs", region_name=region)

    async def spawn(self, bug_id: str, project_id: str, project_web_url: str, job_id: str,
                    source_branch: str = "") -> WorkerEntry:
        if self._registry.exists(bug_id):
            logger.warning("[EcsSpawner] bug_id=%s already running, skip", bug_id)
            BUG_ID_COLLISIONS.inc()
            return self._registry.get(bug_id)

        entry = await self._start_task(bug_id, project_id, project_web_url, job_id,
                                       source_branch=source_branch)
        self._registry.register(entry)
        WORKERS_SPAWNED.labels(spawner="ecs").inc()
        return entry

    async def restart(self, bug_id: str, project_id: str, project_web_url: str, job_id: str,
                      source_branch: str = "") -> WorkerEntry:
        old = self._registry.get(bug_id)
        restart_count = (old.restart_count + 1) if old else 1

        if old and old.process:
            try:
                old.process.terminate()
            except Exception as e:
                logger.warning("[EcsSpawner] terminate bug_id=%s: %s", bug_id, e)

        entry = await self._start_task(bug_id, project_id, project_web_url, job_id,
                                       source_branch=source_branch,
                                       restart_count=restart_count)
        self._registry.register(entry)
        logger.info("[EcsSpawner] restarted bug_id=%s restart_count=%d", bug_id, restart_count)
        return entry

    async def _start_task(self, bug_id: str, project_id: str, project_web_url: str,
                          job_id: str, source_branch: str = "", restart_count: int = 0) -> WorkerEntry:
        environment = {
            "BUG_ID": bug_id,
            "REDIS_URL": self._redis_url,
            "project_id": project_id,
            "project_web_url": project_web_url,
            "job_id": job_id,
            # Empty string keeps the historical default ("main") in the worker.
            "BUG_SOURCE_BRANCH": source_branch or "",
            # Worker uses the same GitLab-auth env as host-mode gitlab.com runs
            # (HTTPS + oauth2:<token>, no SSH). The spawner choice is decoupled
            # from this via WORKER_SPAWNER on the orchestrator side.
            "ENV": self._worker_env,
            # Ephemeral task → checkpoint has no resume value AND re-creates
            # the shared-state contamination hazard (same rationale as the
            # Docker spawner). "none" == pre-checkpointing behaviour.
            "BF_CHECKPOINT_BACKEND": "none",
        }
        # Worker-side knobs that must survive the spawn boundary. Conditional so
        # an unset variable leaves the container spec byte-identical to before
        # (same "empty means unchanged" convention as journal_host_path).
        # MINI_IMPL / BF_STEP_CHECKPOINT select the vendored mini loop and its
        # intra-loop resume (plan item W2).
        for var in ("MINI_IMPL", "BF_STEP_CHECKPOINT", "BF_AGENT_CONFIG"):
            if os.getenv(var):
                environment[var] = os.environ[var]

        run_kwargs = {
            "cluster": self._cluster,
            "taskDefinition": self._task_def,
            "launchType": "EC2",
            "count": 1,
            "overrides": {
                "containerOverrides": [{
                    "name": "bf-worker",
                    # entrypoint.sh execs `python bf_worker.py "$@"` so the
                    # bug_id must arrive via command override (the Docker
                    # spawner does the same: command=["--bug-id", bug_id]).
                    "command": ["--bug-id", bug_id],
                    "environment": [
                        {"name": k, "value": v} for k, v in environment.items()
                    ],
                }]
            },
        }
        if self._worker_network_mode == "awsvpc":
            # awsvpc tasks need their own ENI; bridge/host inherit the host's
            # networking and ECS rejects networkConfiguration for them
            # (InvalidParameterException). On EC2 launch type secondary awsvpc
            # ENIs do NOT inherit MapPublicIpOnLaunch — they get no public IP
            # by default; need NAT or EIP for outbound. Single-host setups
            # usually do better with "host" (shares the host's primary ENI).
            run_kwargs["networkConfiguration"] = {
                "awsvpcConfiguration": {
                    "subnets": self._subnets,
                    "securityGroups": self._security_groups,
                    # `assignPublicIp` is Fargate-only — passing it on EC2
                    # launch type returns InvalidParameterException.
                }
            }

        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: self._ecs.run_task(**run_kwargs),
        )

        tasks = resp.get("tasks", [])
        if not tasks:
            raise RuntimeError(f"ecs:RunTask returned no tasks for bug_id={bug_id}")

        task_arn = tasks[0]["taskArn"]
        proxy = EcsTaskProxy(self._ecs, self._cluster, task_arn)
        logger.info("[EcsSpawner] started bug_id=%s task=%s", bug_id, task_arn)

        now = time.time()
        return WorkerEntry(
            bug_id=bug_id,
            process=proxy,
            project_id=project_id,
            project_web_url=project_web_url,
            job_id=job_id,
            source_branch=source_branch,
            started_at=now,
            warmup_deadline=now + WARMUP_GRACE,
            restart_count=restart_count,
        )


class K8sJobProxy:
    """
    Wraps a k8s Job to provide an interface compatible with
    asyncio.subprocess.Process (pid, returncode, terminate, kill, wait)
    plus reload_status() (HealthMonitor refreshes via it, same as Docker).
    """

    # A Job whose pod never gets scheduled would otherwise cost one pod LIST
    # per HealthMonitor tick, forever. 20 attempts is minutes of grace and then
    # it stops asking.
    _MAX_NODE_LOOKUPS = 20

    def __init__(self, batch_api, job_name: str, namespace: str, core_api=None):
        self._batch = batch_api
        self._job_name = job_name
        self._namespace = namespace
        self._returncode = None
        # Which node the pod landed on (W4/D7). Cached on first sighting so a
        # restart can steer the replacement back there even after the Job and
        # its pod are gone — the W2.5 evaluation container and step-checkpoint
        # records are node-local, so resume only works on the original node.
        self._core = core_api
        self._node_name = None
        self._node_lookups = 0

    @property
    def pid(self):
        """The Job name doubles as a human-readable pseudo-pid."""
        return self._job_name

    @property
    def node_name(self) -> str:
        return self._node_name or ""

    def lookup_node(self) -> str:
        """Read (once) which node this Job's pod is on. Never raises.

        Called from reload_status(), which the HealthMonitor runs every tick —
        so a transient API error or a missing RBAC verb must degrade to "we
        don't know the node" and nothing else. Getting this wrong would break
        exit-code detection, which is far more important than the affinity
        hint it feeds.
        """
        if self._core is None or self._node_name or self._node_lookups >= self._MAX_NODE_LOOKUPS:
            return self.node_name
        self._node_lookups += 1
        try:
            pods = self._core.list_namespaced_pod(
                namespace=self._namespace,
                label_selector=f"job-name={self._job_name}",
            ).items
        except Exception as e:
            logger.debug("[K8sSpawner] node lookup job=%s: %s", self._job_name, e)
            return ""
        for p in pods:
            node = getattr(getattr(p, "spec", None), "node_name", None)
            if node:
                self._node_name = node
                logger.info("[K8sSpawner] job=%s scheduled on node=%s",
                            self._job_name, node)
                break
        return self.node_name

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
        else:
            # Still running → this is the window in which the pod exists and
            # its nodeName is readable. Cache it now; after a restart deletes
            # the Job the pod is gone and the answer is unrecoverable.
            self.lookup_node()

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
                 job_ttl_seconds: int,
                 host_aliases: list | None = None,
                 journal_host_path: str = "",
                 cpu_request: str = "", mem_request: str = "",
                 cpu_limit: str = "", mem_limit: str = "",
                 ephemeral_storage_request: str = "",
                 docker_sock: str = "",
                 step_checkpoint_host_path: str = "",
                 node_selector: str = "", tolerations: str = "",
                 resume_affinity: str = "preferred"):
        self._registry = registry
        self._redis_url = redis_url
        self._worker_image = worker_image
        self._namespace = namespace
        self._worker_config_map = worker_config_map
        self._secret_name = secret_name
        self._job_ttl_seconds = job_ttl_seconds
        # Optional pod-level hostAliases (list of {"ip":..., "hostnames":[...]})
        # and hostPath journal mount. Both default-empty → produced Job is
        # byte-identical to the pre-2026-05-30 spec (preserves the AWS ECS
        # spawner contract + existing K8s deployments).
        self._host_aliases = host_aliases or []
        self._journal_host_path = journal_host_path

        # ── W4 worker Job shaping. Every one of these is default-empty and
        # conditionally rendered, so an unconfigured deployment still produces
        # the pre-W4 Job spec (labels aside — see _build_job).
        self._requests = self._quantities({
            "cpu": cpu_request,
            "memory": mem_request,
            "ephemeral-storage": ephemeral_storage_request,
        }, "request")
        self._limits = self._quantities({
            "cpu": cpu_limit,
            "memory": mem_limit,
        }, "limit")
        if self._limits and not self._requests:
            # Legal (k8s defaults requests to limits) but almost always a typo,
            # and it silently makes the pod Guaranteed-ish with a request the
            # operator never chose.
            logger.warning("[K8sSpawner] worker limits set without requests — k8s will "
                           "derive requests from limits; set them explicitly instead")
        self._docker_sock = docker_sock
        self._step_checkpoint_host_path = step_checkpoint_host_path
        self._node_selector = _parse_json_setting(
            node_selector, dict, "K8S_WORKER_NODE_SELECTOR")
        self._tolerations = _parse_json_setting(
            tolerations, list, "K8S_WORKER_TOLERATIONS")
        mode = (resume_affinity or "preferred").strip().lower()
        if mode not in _RESUME_AFFINITY_MODES:
            # Fatal on purpose: a typo here degrades silently to "no affinity",
            # which turns W2.5's exactly-once into a full re-run on every
            # restart with nothing in the logs to say so.
            raise ValueError(
                f"K8S_WORKER_RESUME_AFFINITY={resume_affinity!r} is not valid "
                f"(expected one of {'|'.join(_RESUME_AFFINITY_MODES)})"
            )
        self._resume_affinity = mode

        from kubernetes import client, config
        from kubernetes.config.config_exception import ConfigException
        try:
            config.load_incluster_config()  # orchestrator pod's service account
            logger.info("[K8sSpawner] using in-cluster config")
        except ConfigException:
            config.load_kube_config()        # dev: orchestrator run outside k8s
            logger.info("[K8sSpawner] using local kubeconfig")
        self._batch = client.BatchV1Api()
        # Pods are read only to answer "which node did this Job land on"
        # (restart affinity). The existing Role already grants pods get/list.
        self._core = client.CoreV1Api()

    @staticmethod
    def _quantities(values: dict, kind: str) -> dict | None:
        """Validate + collect the non-empty resource quantities.

        Raises at construction rather than at spawn time — see _QUANTITY_RE.
        """
        out = {}
        for key, raw in values.items():
            raw = (raw or "").strip()
            if not raw:
                continue
            if not _QUANTITY_RE.match(raw):
                raise ValueError(
                    f"worker {key} {kind} {raw!r} is not a valid k8s quantity "
                    f"(e.g. '3', '500m', '4Gi')"
                )
            out[key] = raw
        return out or None

    async def spawn(self, bug_id: str, project_id: str, project_web_url: str, job_id: str,
                    source_branch: str = "") -> WorkerEntry:
        if self._registry.exists(bug_id):
            logger.warning("[K8sSpawner] bug_id=%s already running, skip", bug_id)
            BUG_ID_COLLISIONS.inc()
            return self._registry.get(bug_id)

        entry = await self._start_job(bug_id, project_id, project_web_url, job_id,
                                      source_branch=source_branch)
        self._registry.register(entry)
        WORKERS_SPAWNED.labels(spawner="k8s").inc()
        return entry

    async def restart(self, bug_id: str, project_id: str, project_web_url: str, job_id: str,
                      source_branch: str = "") -> WorkerEntry:
        old = self._registry.get(bug_id)
        restart_count = (old.restart_count + 1) if old else 1

        # ORDER MATTERS: read the node BEFORE terminating. terminate() deletes
        # the Job with Background propagation, so its pod disappears
        # asynchronously and with it the only record of where this bug was
        # running. W2.5's resume needs that node (the evaluation container and
        # the step-checkpoint records are both node-local).
        node_hint = ""
        if old and old.process is not None:
            try:
                node_hint = old.process.lookup_node()
            except AttributeError:
                pass    # non-k8s proxy in a test fixture
            except Exception as e:
                logger.debug("[K8sSpawner] node hint for bug_id=%s: %s", bug_id, e)

        if old and old.process:
            try:
                old.process.terminate()
            except Exception as e:
                logger.warning("[K8sSpawner] terminate bug_id=%s: %s", bug_id, e)

        if node_hint:
            logger.info("[K8sSpawner] bug_id=%s restarting with node preference %s",
                        bug_id, node_hint)
        entry = await self._start_job(bug_id, project_id, project_web_url, job_id,
                                      source_branch=source_branch,
                                      restart_count=restart_count,
                                      node_hint=node_hint)
        self._registry.register(entry)
        logger.info("[K8sSpawner] restarted bug_id=%s restart_count=%d", bug_id, restart_count)
        return entry

    def _build_job(self, job_name: str, bug_id: str, project_id: str,
                   project_web_url: str, job_id: str, source_branch: str = "",
                   node_hint: str = ""):
        from kubernetes import client

        env = [
            client.V1EnvVar(name="BUG_ID", value=bug_id),
            client.V1EnvVar(name="project_id", value=project_id),
            client.V1EnvVar(name="project_web_url", value=project_web_url),
            client.V1EnvVar(name="job_id", value=job_id),
            client.V1EnvVar(name="REDIS_URL", value=self._redis_url),
            # Empty string keeps the historical default ("main") in the worker.
            client.V1EnvVar(name="BUG_SOURCE_BRANCH", value=source_branch or ""),
            # Per-bug Job pod is ephemeral — sqlite checkpoint has no resume
            # value here AND re-creates the shared-state contamination hazard
            # (project invariant #4). Same rationale as Docker/ECS spawners.
            client.V1EnvVar(name="BF_CHECKPOINT_BACKEND", value="none"),
        ]
        # Worker-side knobs that must survive the spawn boundary. Conditional so
        # an unset variable leaves the container spec byte-identical to before
        # (same "empty means unchanged" convention as journal_host_path).
        # MINI_IMPL / BF_STEP_CHECKPOINT select the vendored mini loop and its
        # intra-loop resume (plan item W2).
        # BF_STEP_LEDGER selects W2.5's exactly-once command ledger (and is the
        # A/B control arm); BF_MINI_CONTAINER_TIMEOUT is the ONLY way ver99 can
        # size the resume window (it has no --config overlay to layer);
        # BF_MAX_COST_USD is the only cap that bounds spend rather than work.
        # NOT forwarded: BF_STEP_CHECKPOINT_DIR — that one is owned by the
        # hostPath mount below. Forwarding the orchestrator's own value would
        # hand the worker a path that does not exist in its pod, and the step
        # store is startup-strict, so EVERY worker would die before its first
        # LLM call.
        for var in ("MINI_IMPL", "BF_STEP_CHECKPOINT", "BF_STEP_LEDGER",
                    "BF_MINI_CONTAINER_TIMEOUT", "BF_MAX_COST_USD",
                    "BF_AGENT_CONFIG"):
            if os.getenv(var):
                env.append(client.V1EnvVar(name=var, value=os.environ[var]))
        # Surface the journal mount to the worker code path. The worker reads
        # BF_JOURNAL_DIR (see bf_worker/journal.py) and writes record.json
        # under it. Empty journal_host_path → variable not set → journal lives
        # in the ephemeral Pod fs (pre-existing behaviour, dies with the Pod).
        volume_mounts = []
        volumes = []
        if self._journal_host_path:
            env.append(client.V1EnvVar(name="BF_JOURNAL_DIR",
                                       value=self._journal_host_path))
            volume_mounts.append(client.V1VolumeMount(
                name="journal",
                mount_path=self._journal_host_path,
            ))
            volumes.append(client.V1Volume(
                name="journal",
                host_path=client.V1HostPathVolumeSource(
                    path=self._journal_host_path,
                    # DirectoryOrCreate auto-creates on the node — avoids a
                    # bootstrap chicken-and-egg where setup.sh would have to
                    # mkdir on the host before the first worker scheduled.
                    type="DirectoryOrCreate",
                ),
            ))
        # W2.5's intra-loop step checkpoint (plan item W4). Same pattern as the
        # journal mount, and node-local for the same reason the resume itself
        # is: the evaluation container these records point at lives on this
        # node's dockerd, so "record readable" and "container attachable" are
        # true together or not at all. Without this the records land in the
        # Pod's own $HOME and W2.5 does nothing on k8s.
        if self._step_checkpoint_host_path:
            env.append(client.V1EnvVar(name="BF_STEP_CHECKPOINT_DIR",
                                       value=self._step_checkpoint_host_path))
            volume_mounts.append(client.V1VolumeMount(
                name="step-checkpoint",
                mount_path=self._step_checkpoint_host_path,
            ))
            volumes.append(client.V1Volume(
                name="step-checkpoint",
                host_path=client.V1HostPathVolumeSource(
                    path=self._step_checkpoint_host_path,
                    type="DirectoryOrCreate",
                ),
            ))
        # Host docker socket, so mini can drive the node's dockerd to run its
        # sibling evaluation container (ver99 in a pod).
        #
        # ⚠️ This works ONLY because the worker container runs as root: the
        #    socket is root:docker 0660 and the docker GID differs per node
        #    (ls4900=137, minus=980, both measured), so no single
        #    supplementalGroups value can be correct on both. Adding a
        #    runAsNonRoot / runAsUser securityContext here would break ver99
        #    with a permission-denied buried in mini's stderr. Pinned by a test.
        if self._docker_sock:
            volume_mounts.append(client.V1VolumeMount(
                name="docker-sock",
                mount_path=self._docker_sock,
            ))
            volumes.append(client.V1Volume(
                name="docker-sock",
                host_path=client.V1HostPathVolumeSource(
                    path=self._docker_sock,
                    # `Socket` rather than `File`: mistyping the path then fails
                    # at mount time with a clear reason instead of handing the
                    # container an empty file.
                    type="Socket",
                ),
            ))

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
            # Empty list → kubernetes client serializes to None → byte-identical
            # to the pre-2026-05-30 Job spec when no journal mount is set.
            volume_mounts=volume_mounts or None,
            # Both None when unset → no `resources` key at all → BestEffort QoS,
            # exactly as before W4. When set, note what the request MEANS for a
            # ver99 worker: it books node budget on behalf of mini's sibling
            # evaluation container, which the scheduler cannot see. See
            # settings/orchestrator_settings.py and docs/k3s.md.
            resources=client.V1ResourceRequirements(
                requests=self._requests, limits=self._limits,
            ) if (self._requests or self._limits) else None,
        )
        # Translate the operator-supplied dicts into V1HostAlias objects. Each
        # item is {"ip": str, "hostnames": [str]}; same shape as the K8s API.
        host_aliases = [
            client.V1HostAlias(ip=a["ip"], hostnames=a["hostnames"])
            for a in self._host_aliases
        ] or None
        tolerations = [
            _toleration(t) for t in (self._tolerations or []) if isinstance(t, dict)
        ] or None
        pod_spec = client.V1PodSpec(
            # `Never` + backoffLimit=0 below are NOT conservative defaults —
            # together they are the declaration that the orchestrator's
            # HealthMonitor is the SINGLE owner of restart policy. Handing any
            # of it back to the Job controller creates two owners: during a
            # kubelet CrashLoopBackOff (10s→20s→40s…) the heartbeat key expires
            # at 60s, the monitor spawns a replacement Job, and the backed-off
            # pod then starts too — two workers on one bug_id, which for ver99
            # means two processes `docker exec`-ing into the SAME evaluation
            # container and sharing one /.sdlcma ledger. That silently breaks
            # W2.5's exactly-once. Pinned by tests; see docs/architecture.md.
            restart_policy="Never",
            # The worker talks only to Redis / GitLab / the LLM — never the
            # k8s API. Don't mount the (default) SA token: removes a useless
            # credential a hijacked worker could otherwise reach for. Defense
            # in depth alongside the patch/fetch/prompt guards.
            automount_service_account_token=False,
            containers=[container],
            host_aliases=host_aliases,
            volumes=volumes or None,
            # All three default to None → pod spec identical to pre-W4.
            node_selector=self._node_selector or None,
            tolerations=tolerations,
            affinity=self._resume_affinity_spec(node_hint),
        )
        labels = self._labels(job_name, bug_id)
        template = client.V1PodTemplateSpec(
            metadata=client.V1ObjectMeta(labels=labels),
            spec=pod_spec,
        )
        return client.V1Job(
            metadata=client.V1ObjectMeta(
                name=job_name,
                namespace=self._namespace,
                labels=labels,
            ),
            spec=client.V1JobSpec(
                # See the restart_policy comment above: this is half of the
                # single-owner contract, not a tunable.
                backoff_limit=0,                              # orchestrator owns retries
                ttl_seconds_after_finished=self._job_ttl_seconds,
                template=template,
            ),
        )

    @staticmethod
    def _labels(job_name: str, bug_id: str) -> dict:
        """Labels for the Job and its pod template.

        `bug-id` carries the actual bug_id. Until W4 it carried the JOB NAME —
        a name that meant something else than it said, which nothing in the
        repo selected on (every script uses `app=bf-worker`) but which would
        have become load-bearing the moment we started querying by label.
        `job-name` is the query key for "the pod of THIS Job" (including the
        -rN restart suffix); we set it ourselves rather than relying on the
        controller-injected `batch.kubernetes.io/job-name`, whose name has
        changed across k8s versions while this chart serves both 1.35 and 1.36.
        """
        return {
            "app": "bf-worker",
            "job-name": job_name,
            "bug-id": _k8s_label_value(bug_id),
        }

    def _resume_affinity_spec(self, node_hint: str):
        """Soft-steer a RESTARTED worker back to the node it died on.

        Only rendered when all three hold — otherwise the pod spec is
        unchanged from pre-W4:
          * resume is actually enabled for these workers (else there is
            nothing on that node worth going back for),
          * we know where the previous attempt ran (a cold spawn does not),
          * the operator has not turned it off.

        `preferred`, not `required`, is the whole point: if the node is gone
        the pod still schedules somewhere and cold-starts — FileStore.load()
        simply finds no record there, which is correct behaviour, just more
        expensive. A required affinity would leave the pod Pending forever, and
        since backoffLimit=0 means the Job never fails, MAX_WORKER_RESTARTS
        would never fire either: the bug would vanish without a trace.
        """
        if not node_hint or self._resume_affinity == "off" or not _worker_resume_enabled():
            return None
        from kubernetes import client
        term = client.V1NodeSelectorTerm(
            match_expressions=[client.V1NodeSelectorRequirement(
                key="kubernetes.io/hostname", operator="In", values=[node_hint])],
        )
        if self._resume_affinity == "required":
            return client.V1Affinity(node_affinity=client.V1NodeAffinity(
                required_during_scheduling_ignored_during_execution=
                    client.V1NodeSelector(node_selector_terms=[term])))
        return client.V1Affinity(node_affinity=client.V1NodeAffinity(
            preferred_during_scheduling_ignored_during_execution=[
                client.V1PreferredSchedulingTerm(weight=100, preference=term)]))

    async def _start_job(self, bug_id: str, project_id: str, project_web_url: str,
                          job_id: str, source_branch: str = "", restart_count: int = 0,
                          node_hint: str = "") -> WorkerEntry:
        from kubernetes.client.rest import ApiException

        job_name = _k8s_job_name(bug_id, restart_count)
        job = self._build_job(job_name, bug_id, project_id, project_web_url, job_id,
                              source_branch=source_branch, node_hint=node_hint)

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

        proxy = K8sJobProxy(self._batch, job_name, self._namespace, self._core)
        logger.info("[K8sSpawner] started bug_id=%s job=%s", bug_id, job_name)

        now = time.time()
        return WorkerEntry(
            bug_id=bug_id,
            process=proxy,
            project_id=project_id,
            project_web_url=project_web_url,
            job_id=job_id,
            source_branch=source_branch,
            started_at=now,
            warmup_deadline=now + WARMUP_GRACE,
            restart_count=restart_count,
        )
