"""
OrchestratorSettings: two-step configuration loading.

Step 1 — EnvProbe: reads only the ENV variable (or root .env), gets the current environment name.
Step 2 — OrchestratorSettings: loads the corresponding orchestrator_<env>.env by environment name,
          then stacks environment variables on top.

Priority (high → low):
  environment variables > orchestrator_<env>.env > code defaults

Usage:
  from settings.orchestrator_settings import cfg
  cfg.redis_url
"""
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from .base_settings import BaseAppSettings

BASE_DIR = Path(__file__).resolve().parent  # …/settings/


# ── Step 1: probe, read ENV only ─────────────────────────────

class _EnvProbe(BaseSettings):
    """Only used to probe the ENV field; not exposed externally."""
    env: str = "local"
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",   # shared .env (may not exist)
        extra="ignore",
    )


_probe = _EnvProbe()


# ── Step 2: full configuration ────────────────────────────────

class OrchestratorSettings(BaseAppSettings):
    """
    Orchestrator-specific configuration.
    Valid env values and corresponding files:
      local               → orchestrator_local.env
      local_multi_process → orchestrator_local_multi_process.env
      test                → orchestrator_test.env
      production          → orchestrator_production.env
    """
    # ── worker spawner selection ───────────────────────────────
    # Decouples "where the worker runs" from "which GitLab" (env). Orchestrator
    # -only (gateway/worker never spawn) so scoped here, not in base_settings.
    #   "" / "auto" → derive from env (back-compat: docker for the
    #                 local_docker_compose* envs, k8s for local_k8s, else
    #                 subprocess) — existing envs/tests byte-identical.
    #   "docker"    → DockerWorkerSpawner (Docker API, one container per bug)
    #   "process"   → WorkerSpawner (subprocess)
    #   "k8s"       → K8sJobSpawner
    #   "ecs"       → EcsWorkerSpawner (ecs:RunTask, one task per bug). ENV
    #                 stays "gitlab_saas" so the worker still uses the
    #                 gitlab.com HTTPS+oauth2 auth path; only the spawner
    #                 differs from the host-mode gitlab.com run.
    # Stage-2 (gitlab_saas in containers) sets WORKER_SPAWNER=docker so the
    # gitlab.com env still spawns worker containers; host-mode gitlab_saas
    # (infra/aws-gitlab, no Docker socket) leaves it unset → subprocess.
    worker_spawner: str = ""

    # ── k8s Job mode (local_k8s) ───────────────────────────────
    # Read only by orchestrator.spawner.K8sJobSpawner when env == "local_k8s".
    # Scoped here (not base_settings) because the orchestrator is the only
    # component that spawns worker Jobs; gateway/worker never read these.
    # Overridden by orchestrator_local_k8s.env / the orchestrator-config
    # ConfigMap via the usual priority (process env > .env > default).
    k8s_namespace: str = "sdlcma"
    k8s_worker_config_map: str = "worker-config"
    k8s_secret_name: str = "sdlcma-secrets"
    # Finished Jobs are GC'd by the k8s TTL controller this many seconds after
    # completion — keeps `kubectl get jobs` readable without an explicit reaper.
    k8s_job_ttl_seconds: int = 600
    # Optional pod-level hostAliases injected into every spawned bf-worker
    # Job. JSON string (env-friendly) of the standard K8s hostAlias shape:
    # `[{"ip":"100.x.x.x","hostnames":["minus"]}]`. Empty = no aliases (the
    # historical default). Use case: cluster pods need to resolve a hostname
    # that's only reachable on the operator's tailnet (e.g. self-hosted
    # GitLab "minus" behind tailscale). setup.sh detects the tailscale IP at
    # deploy time and injects it via the orchestrator ConfigMap.
    k8s_host_aliases: str = ""
    # Optional node-level path mounted RW into every spawned bf-worker Job at
    # the same path, and exported as BF_JOURNAL_DIR. Empty = no mount (the
    # historical default — journal stays inside the ephemeral Pod and dies
    # with it). Single-node-kind convenience: pods schedule on the same node,
    # hostPath is shared. Multi-node clusters need RWX (NFS / CSI) — flip
    # this off and use a real PVC there.
    k8s_journal_host_path: str = ""

    # ── k8s worker Job shaping (plan item W4) ──────────────────
    # All default-empty / inert, so the Job spec K8sJobSpawner produces is
    # byte-identical to the pre-W4 one until an operator opts in. Same "empty
    # means unchanged" convention as k8s_journal_host_path above.

    # Resource requests/limits for the worker container. NOTE the semantics:
    # for a ver99 (SWE-bench) worker these are NOT that process's own usage.
    # mini runs its evaluation container as a *sibling* on the host dockerd,
    # which the kube scheduler cannot see at all, so the request is a proxy
    # booking of the node budget on that sibling's behalf: node allocatable /
    # per-instance footprint = the real concurrency ceiling. See docs/k3s.md.
    #
    # Limits default to empty ON PURPOSE. A limit only constrains the worker
    # process (a few hundred MB); the container that actually eats the node is
    # in another cgroup, so a limit buys no node protection while adding a way
    # to OOMKill the worker when it loads a large trace.
    k8s_worker_cpu_request: str = ""
    k8s_worker_mem_request: str = ""
    k8s_worker_cpu_limit: str = ""
    k8s_worker_mem_limit: str = ""
    k8s_worker_ephemeral_storage_request: str = ""

    # Host docker socket to mount into every worker Job at the same path.
    # Empty = not mounted (historical default). Non-empty enables ver99 in a
    # pod: mini shells out to `docker` to start/exec its evaluation container.
    # ⚠️ Mounting it is equivalent to giving the pod root on the node, and it
    #    REQUIRES the container to keep running as root — the socket is
    #    root:docker 0660 and the docker GID differs per node (ls4900=137,
    #    minus=980 — both measured), so a hardcoded supplementalGroups can
    #    never be right on both. See docs/k3s.md.
    k8s_worker_docker_sock: str = ""

    # Node path for the W2.5 intra-loop step checkpoint, mounted into every
    # worker Job and exported as BF_STEP_CHECKPOINT_DIR. Empty = the records
    # live in the Pod's own $HOME and die with it, i.e. W2.5 resume does not
    # work on k8s at all. Deliberately node-local: the evaluation container the
    # records point at is node-local too, so "record readable" and "container
    # attachable" stay true together.
    k8s_worker_step_checkpoint_host_path: str = ""

    # Scheduling. JSON strings (env-friendly), same style as k8s_host_aliases.
    #   node_selector: '{"kubernetes.io/hostname":"ls4900"}'
    #   tolerations:   '[{"key":"node.kubernetes.io/unreachable",...}]'
    # Malformed JSON is logged and treated as empty (never fatal) — matching
    # the existing k8s_host_aliases behaviour.
    k8s_worker_node_selector: str = ""
    k8s_worker_tolerations: str = ""

    # Where to put a RESTARTED worker (W2.5's records + evaluation container
    # are node-local, so a restart that lands elsewhere silently loses resume).
    #   preferred (default) — soft node affinity: go back if the node is
    #       usable, otherwise schedule anywhere and cold-start. The failure
    #       mode is "more expensive", never "wrong".
    #   required — hard affinity. If the node is gone the pod stays Pending
    #       forever and, with backoffLimit=0, the Job never fails, so
    #       MAX_WORKER_RESTARTS never fires and the bug silently disappears.
    #       Single-node experiments only.
    #   off — never emit affinity.
    # Inert unless the run is resumable (BF_STEP_CHECKPOINT set to something
    # other than "none") AND the previous node is known.
    k8s_worker_resume_affinity: str = "preferred"

    # ── ECS mode ─────────────────────────────────────────────────
    # Read only by orchestrator.spawner.EcsWorkerSpawner when
    # WORKER_SPAWNER=ecs. ENV stays "gitlab_saas" on the orchestrator side
    # (we still talk to gitlab.com); WORKER_SPAWNER decouples *where the
    # worker runs* from *which GitLab*, same pattern as Stage-2 docker.
    # Overridden by orchestrator_ecs.env via the usual priority.
    ecs_cluster_name: str = "sdlcma-cluster"
    ecs_worker_task_def: str = "bf-worker"
    ecs_worker_subnets: str = ""       # comma-separated subnet IDs
    ecs_worker_security_groups: str = ""  # comma-separated SG IDs
    ecs_region: str = "us-east-1"
    # ENV the spawned bf-worker container runs under. Default = gitlab_saas
    # (gitlab.com HTTPS + oauth2 token, no SSH) — the only worker-side env that
    # currently maps cleanly onto cloud GitLab.
    ecs_worker_env: str = "gitlab_saas"
    # Network mode of the bf-worker task definition. Default "host" because a
    # secondary awsvpc ENI in a public subnet does NOT auto-assign a public
    # IP (only the host's *primary* ENI honours MapPublicIpOnLaunch — for
    # secondary ENIs you'd need an EIP or NAT, neither of which is free on AWS).
    # Host mode shares the EC2 host's primary ENI public IP, so the worker can
    # reach gitlab.com / the LLM API without extra cost. Set to "awsvpc" only
    # if the worker needs an isolated ENI AND you've arranged NAT/EIP.
    ecs_worker_network_mode: str = "host"
    # Redis URL the spawned worker uses. With network_mode=host the worker
    # shares the host's network namespace, so the orchestrator's own
    # `redis://localhost:6379/0` works for the worker too — leave empty to
    # fall back. With awsvpc the worker's localhost is its own ENI, so set
    # this to the EC2 host's private IPv4 (e.g. via CloudFormation).
    ecs_worker_redis_url: str = ""

    # ── distr-pull mode ─────────────────────────────────────────
    # Read only by orchestrator.dispatcher.DistributedDispatcher and
    # orchestrator.daemon_monitor.DaemonMonitor when WORKER_SPAWNER=distr-pull.
    # Scoped here (not base_settings) because only the orchestrator reads them;
    # daemons receive these values via their own CLI / env.
    worker_daemon_cache_ttl: int = 5         # seconds before daemon state cache refreshes
    worker_daemon_recovery_interval: int = 15  # seconds between DaemonMonitor ticks
    worker_daemon_hb_ttl: int = 30           # seconds before daemon heartbeat key expires
    worker_daemon_hb_interval: int = 10      # seconds between daemon heartbeat writes

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / f"orchestrator_{_probe.env}.env",
        env_file_encoding="utf-8",
        extra="ignore",          # ignore undefined fields in .env
    )


# Module-level singleton — entire process shares the same configuration
cfg: OrchestratorSettings = OrchestratorSettings()
