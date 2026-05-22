"""Unit tests for orchestrator.spawner.K8sJobSpawner (1a.3).

Strategy: the kubernetes *client models* (V1Job, V1Container, …) are real —
`kubernetes` is a hard dep of the orchestrator — but the API surface
(BatchV1Api) and kubeconfig loading are mocked, so these run with no cluster.
We assert on the V1Job object the spawner hands to create_namespaced_job(),
because that body IS the contract with the k8s API.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kubernetes.client.rest import ApiException  # noqa: E402

from orchestrator.registry import WorkerRegistry  # noqa: E402
from orchestrator.spawner import (  # noqa: E402
    K8sJobProxy,
    K8sJobSpawner,
    _k8s_job_name,
)

NS = "sdlcma"


def _make_spawner(registry=None, batch=None):
    """Build a K8sJobSpawner with kubeconfig + BatchV1Api mocked out."""
    registry = registry or WorkerRegistry()
    batch = batch or MagicMock()
    with patch("kubernetes.config.load_incluster_config",
               side_effect=__import__("kubernetes").config.config_exception.ConfigException), \
         patch("kubernetes.config.load_kube_config"), \
         patch("kubernetes.client.BatchV1Api", return_value=batch):
        spawner = K8sJobSpawner(
            registry=registry,
            redis_url="redis://redis:6379/0",
            worker_image="dh-bf-worker:latest",
            namespace=NS,
            worker_config_map="worker-config",
            secret_name="sdlcma-secrets",
            job_ttl_seconds=600,
        )
    return spawner, registry, batch


# ── _k8s_job_name (pure) ─────────────────────────────────────────

@pytest.mark.parametrize("bug_id,restart,expected", [
    ("2026_05_15-12_30_45_3", 0, "bf-worker-2026-05-15-12-30-45-3"),
    ("2026_05_15-12_30_45_3", 2, "bf-worker-2026-05-15-12-30-45-3-r2"),
    ("BUG-Local-1", 0, "bf-worker-bug-local-1"),
    ("__weird__id__", 0, "bf-worker-weird-id"),
])
def test_k8s_job_name(bug_id, restart, expected):
    assert _k8s_job_name(bug_id, restart) == expected


def test_k8s_job_name_is_dns1123_and_capped():
    name = _k8s_job_name("x" * 200, 7)
    assert len(name) <= 63
    assert not name.endswith("-")
    assert name == name.lower()


# ── spawn ────────────────────────────────────────────────────────

def test_spawn_creates_job_with_expected_spec():
    spawner, registry, batch = _make_spawner()

    entry = asyncio.run(spawner.spawn(
        bug_id="2026_05_15-12_30_45_3",
        project_id="42",
        project_web_url="http://gitlab/x.git",
        job_id="99",
    ))

    assert batch.create_namespaced_job.call_count == 1
    _, kwargs = batch.create_namespaced_job.call_args
    assert kwargs["namespace"] == NS
    job = kwargs["body"]

    assert job.metadata.name == "bf-worker-2026-05-15-12-30-45-3"
    assert job.spec.backoff_limit == 0
    assert job.spec.ttl_seconds_after_finished == 600

    pod = job.spec.template.spec
    assert pod.restart_policy == "Never"
    # worker never calls the k8s API → no SA token mounted (hardening)
    assert pod.automount_service_account_token is False
    c = pod.containers[0]
    assert c.image == "dh-bf-worker:latest"
    assert c.image_pull_policy == "IfNotPresent"
    assert c.args == ["--bug-id", "2026_05_15-12_30_45_3"]

    env = {e.name: e.value for e in c.env}
    assert env["BUG_ID"] == "2026_05_15-12_30_45_3"
    assert env["project_id"] == "42"
    assert env["project_web_url"] == "http://gitlab/x.git"
    assert env["job_id"] == "99"
    assert env["REDIS_URL"] == "redis://redis:6379/0"
    # Ephemeral Job → checkpointing must be OFF (invariant #4; parity with
    # DockerWorkerSpawner / EcsWorkerSpawner).
    assert env["BF_CHECKPOINT_BACKEND"] == "none"

    cm_refs = [s.config_map_ref.name for s in c.env_from if s.config_map_ref]
    secret_refs = [s.secret_ref.name for s in c.env_from if s.secret_ref]
    assert "worker-config" in cm_refs
    assert "sdlcma-secrets" in secret_refs

    # registered + pid is the job name
    assert registry.exists("2026_05_15-12_30_45_3")
    assert entry.pid == "bf-worker-2026-05-15-12-30-45-3"


def test_spawn_skips_when_already_running():
    spawner, registry, batch = _make_spawner()
    asyncio.run(spawner.spawn("BUG-1", "1", "u", "1"))
    batch.create_namespaced_job.reset_mock()

    asyncio.run(spawner.spawn("BUG-1", "1", "u", "1"))
    batch.create_namespaced_job.assert_not_called()


def test_spawn_adopts_existing_job_on_409():
    batch = MagicMock()
    batch.create_namespaced_job.side_effect = ApiException(status=409)
    spawner, registry, _ = _make_spawner(batch=batch)

    entry = asyncio.run(spawner.spawn("BUG-9", "1", "u", "1"))  # must not raise
    assert entry.pid == "bf-worker-bug-9"
    assert registry.exists("BUG-9")


def test_spawn_propagates_non_409_apierror():
    batch = MagicMock()
    batch.create_namespaced_job.side_effect = ApiException(status=403)
    spawner, _, _ = _make_spawner(batch=batch)

    with pytest.raises(ApiException):
        asyncio.run(spawner.spawn("BUG-X", "1", "u", "1"))


def test_bf_agent_config_env_propagated(monkeypatch):
    monkeypatch.setenv("BF_AGENT_CONFIG", "configs/memory.json")
    spawner, _, batch = _make_spawner()
    asyncio.run(spawner.spawn("BUG-CFG", "1", "u", "1"))

    job = batch.create_namespaced_job.call_args.kwargs["body"]
    env = {e.name: e.value for e in job.spec.template.spec.containers[0].env}
    assert env["BF_AGENT_CONFIG"] == "configs/memory.json"


# ── restart ──────────────────────────────────────────────────────

def test_restart_deletes_old_and_creates_suffixed_job():
    spawner, registry, batch = _make_spawner()
    asyncio.run(spawner.spawn("BUG-7", "1", "u", "1"))

    entry = asyncio.run(spawner.restart("BUG-7", "1", "u", "1"))

    assert entry.restart_count == 1
    assert entry.pid == "bf-worker-bug-7-r1"
    # old job deleted (proxy.terminate → delete_namespaced_job)
    assert batch.delete_namespaced_job.call_count == 1
    assert batch.create_namespaced_job.call_count == 2


# ── K8sJobProxy.reload_status ────────────────────────────────────

def _proxy_with_status(succeeded=None, failed=None, active=None):
    batch = MagicMock()
    batch.read_namespaced_job_status.return_value = SimpleNamespace(
        status=SimpleNamespace(succeeded=succeeded, failed=failed, active=active)
    )
    return K8sJobProxy(batch, "bf-worker-x", NS), batch


def test_proxy_running_returns_none():
    proxy, _ = _proxy_with_status(active=1)
    proxy.reload_status()
    assert proxy.returncode is None


def test_proxy_succeeded_returns_zero():
    proxy, _ = _proxy_with_status(succeeded=1)
    proxy.reload_status()
    assert proxy.returncode == 0


def test_proxy_failed_returns_one():
    proxy, _ = _proxy_with_status(failed=1)
    proxy.reload_status()
    assert proxy.returncode == 1


def test_proxy_404_treated_as_done():
    batch = MagicMock()
    batch.read_namespaced_job_status.side_effect = ApiException(status=404)
    proxy = K8sJobProxy(batch, "bf-worker-gone", NS)
    proxy.reload_status()
    assert proxy.returncode == 0


def test_proxy_terminate_deletes_job():
    batch = MagicMock()
    proxy = K8sJobProxy(batch, "bf-worker-x", NS)
    proxy.terminate()
    assert batch.delete_namespaced_job.call_count == 1
    assert batch.delete_namespaced_job.call_args.kwargs["name"] == "bf-worker-x"
