"""WORKER_SPAWNER decouples 'where the worker runs' from the GitLab env.

Pins:
  * Empty/"auto" reproduces the historical by-env mapping EXACTLY
    (byte-identical back-compat): local_docker_compose* → docker,
    local_k8s → k8s, everything else (incl. gitlab_saas) → process.
  * Explicit WORKER_SPAWNER overrides the env mapping — notably
    gitlab_saas + docker → DockerWorkerSpawner (cloud Stage 2: the
    gitlab.com env still spawns worker *containers*).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.orchestrator import Orchestrator  # noqa: E402


def _cfg(env: str, worker_spawner: str = ""):
    return SimpleNamespace(
        env=env, worker_spawner=worker_spawner,
        redis_url="redis://redis:6379/15",
        gateway_stream="gateway:stream",
        gateway_consumer_group="g", gateway_consumer_name="c",
        worker_inbox_stream_key="worker:{bug_id}:stream",
        dead_letter_stream="dl",
        worker_heartbeat_key="worker:heartbeat:{bug_id}",
        worker_completed_key="worker:completed:{bug_id}",
        health_check_interval=20, stream_block_ms=1000, stream_count=10,
        worker_image="dh-bf-worker:latest", docker_network="sdlcma_net",
        ssh_private_key="",
        k8s_namespace="sdlcma", k8s_worker_config_map="wc",
        k8s_secret_name="s", k8s_job_ttl_seconds=600,
        ecs_cluster_name="sdlcma-cluster", ecs_worker_task_def="bf-worker",
        ecs_worker_subnets="subnet-aaa", ecs_worker_security_groups="sg-aaa",
        ecs_region="us-east-1", ecs_worker_env="gitlab_saas",
        ecs_worker_redis_url="", ecs_worker_network_mode="host",
    )


def _select(env, worker_spawner=""):
    with patch("orchestrator.orchestrator.aioredis.from_url", return_value=MagicMock()), \
         patch("orchestrator.orchestrator.DockerWorkerSpawner") as D, \
         patch("orchestrator.orchestrator.WorkerSpawner") as P, \
         patch("orchestrator.orchestrator.K8sJobSpawner") as K, \
         patch("orchestrator.orchestrator.EcsWorkerSpawner") as E:
        orch = Orchestrator(settings=_cfg(env, worker_spawner))
        if orch._spawner is D.return_value:
            return "docker"
        if orch._spawner is K.return_value:
            return "k8s"
        if orch._spawner is E.return_value:
            return "ecs"
        if orch._spawner is P.return_value:
            return "process"
    return "?"


# ── back-compat: empty/auto == historical by-env mapping ─────────────────────


@pytest.mark.parametrize("env,expected", [
    ("local_multi_process", "process"),
    ("local_docker_compose", "docker"),
    ("local_docker_compose_http", "docker"),
    ("local_k8s", "k8s"),
    ("gitlab_saas", "process"),          # historical: gitlab_saas → subprocess
    ("test", "process"),
])
def test_empty_is_byte_identical_by_env(env, expected):
    assert _select(env, "") == expected


@pytest.mark.parametrize("env", ["local_multi_process", "gitlab_saas", "local_k8s"])
def test_auto_same_as_empty(env):
    assert _select(env, "auto") == _select(env, "")


# ── explicit override ────────────────────────────────────────────────────────


def test_gitlab_saas_docker_override_is_the_stage2_path():
    # The whole point of Stage 2: gitlab.com env, worker still a container.
    assert _select("gitlab_saas", "docker") == "docker"


def test_gitlab_saas_ecs_override_is_the_aws_ecs_path():
    # AWS ECS: gitlab.com env stays, ENV stays "gitlab_saas", only spawner
    # changes — the WORKER_SPAWNER decoupling pattern, applied to AWS.
    # No "ENV=ecs" auto-mapping exists: ECS is reachable ONLY via explicit
    # WORKER_SPAWNER=ecs. This pins that contract.
    assert _select("gitlab_saas", "ecs") == "ecs"


@pytest.mark.parametrize("kind", ["docker", "process", "k8s", "ecs"])
def test_explicit_override_wins_over_env(kind):
    # local_multi_process would auto→process; explicit value overrides.
    assert _select("local_multi_process", kind) == kind


def test_override_is_case_insensitive():
    assert _select("gitlab_saas", "Docker") == "docker"
    assert _select("gitlab_saas", "ECS") == "ecs"
