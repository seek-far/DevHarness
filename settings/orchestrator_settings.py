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

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / f"orchestrator_{_probe.env}.env",
        env_file_encoding="utf-8",
        extra="ignore",          # ignore undefined fields in .env
    )


# Module-level singleton — entire process shares the same configuration
cfg: OrchestratorSettings = OrchestratorSettings()
