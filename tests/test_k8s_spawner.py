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


def _make_spawner(registry=None, batch=None, *, host_aliases=None,
                  journal_host_path="", core=None, **w4):
    """Build a K8sJobSpawner with kubeconfig + BatchV1Api mocked out.

    `host_aliases` / `journal_host_path` default to the pre-2026-05-30 shape
    (no hostAliases, no journal mount) so every existing test exercises the
    byte-identical Job spec. The two new opt-in tests below pass these to
    exercise the additive paths.

    `**w4` forwards the plan-item-W4 knobs (resources / docker_sock /
    step_checkpoint_host_path / node_selector / tolerations /
    resume_affinity). All default-empty, so tests that don't pass them keep
    exercising the pre-W4 spec.
    """
    registry = registry or WorkerRegistry()
    batch = batch or MagicMock()
    core = core or MagicMock()
    with patch("kubernetes.config.load_incluster_config",
               side_effect=__import__("kubernetes").config.config_exception.ConfigException), \
         patch("kubernetes.config.load_kube_config"), \
         patch("kubernetes.client.BatchV1Api", return_value=batch), \
         patch("kubernetes.client.CoreV1Api", return_value=core):
        spawner = K8sJobSpawner(
            registry=registry,
            redis_url="redis://redis:6379/0",
            worker_image="dh-bf-worker:latest",
            namespace=NS,
            worker_config_map="worker-config",
            secret_name="sdlcma-secrets",
            job_ttl_seconds=600,
            host_aliases=host_aliases,
            journal_host_path=journal_host_path,
            **w4,
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


# ── hostAliases + journal hostPath (additive, default no-op) ────────────────
#
# These exercise the 2026-05-30 additions to K8sJobSpawner: optional pod-level
# hostAliases (so worker Jobs can reach `minus` over the operator's tailnet)
# and an optional hostPath journal mount (so runrecord-exporter can read the
# journal that workers write). Both default empty / "" → the produced Job
# spec is byte-identical to the pre-2026-05-30 shape, which the existing
# test_spawn_creates_job_with_expected_spec test already pins.

def test_spawn_no_host_aliases_no_volumes_by_default():
    """Regression guard: with neither new param passed, the Job spec must
    NOT carry hostAliases / volumes / volumeMounts / BF_JOURNAL_DIR. AWS ECS
    and existing K8s deployments depend on this byte-identical default."""
    spawner, _, batch = _make_spawner()
    asyncio.run(spawner.spawn("BUG-DEF", "1", "u", "1"))
    job = batch.create_namespaced_job.call_args.kwargs["body"]
    pod = job.spec.template.spec
    assert pod.host_aliases is None
    assert pod.volumes is None
    c = pod.containers[0]
    assert c.volume_mounts is None
    env = {e.name: e.value for e in c.env}
    assert "BF_JOURNAL_DIR" not in env


def test_spawn_with_host_aliases_injects_pod_field():
    """When host_aliases is non-empty, every spawned Job carries a V1HostAlias
    list translated 1:1 from the operator-supplied dicts. Inside a kind
    cluster this is the mechanism that lets `git clone http://minus:8929/...`
    resolve to the operator's tailscale IP (kindnet SNATs the egress through
    the host's tailscale interface)."""
    spawner, _, batch = _make_spawner(host_aliases=[
        {"ip": "100.64.0.5", "hostnames": ["minus"]},
        {"ip": "100.64.0.6", "hostnames": ["registry", "registry.lan"]},
    ])
    asyncio.run(spawner.spawn("BUG-HA", "1", "u", "1"))
    pod = batch.create_namespaced_job.call_args.kwargs["body"].spec.template.spec
    assert pod.host_aliases is not None
    assert len(pod.host_aliases) == 2
    assert pod.host_aliases[0].ip == "100.64.0.5"
    assert pod.host_aliases[0].hostnames == ["minus"]
    assert pod.host_aliases[1].ip == "100.64.0.6"
    assert pod.host_aliases[1].hostnames == ["registry", "registry.lan"]


def test_spawn_with_journal_host_path_mounts_volume_and_sets_env():
    """When journal_host_path is non-empty, the Pod gets a hostPath Volume
    mounted at the SAME path inside the container, and BF_JOURNAL_DIR points
    there — that's what makes per-worker RunRecord files survive Pod
    termination (Pod is ephemeral; the node-local directory persists), which
    in turn is what lets runrecord-exporter render Prometheus metrics from
    the accumulated journal."""
    spawner, _, batch = _make_spawner(journal_host_path="/var/sdlcma/journal")
    asyncio.run(spawner.spawn("BUG-J", "1", "u", "1"))
    pod = batch.create_namespaced_job.call_args.kwargs["body"].spec.template.spec
    assert pod.volumes is not None
    assert len(pod.volumes) == 1
    vol = pod.volumes[0]
    assert vol.name == "journal"
    assert vol.host_path.path == "/var/sdlcma/journal"
    # DirectoryOrCreate auto-creates on the node so setup.sh / cluster admin
    # doesn't need a pre-bootstrap mkdir step before the first worker runs.
    assert vol.host_path.type == "DirectoryOrCreate"

    c = pod.containers[0]
    mounts = c.volume_mounts
    assert mounts is not None
    assert len(mounts) == 1
    assert mounts[0].name == "journal"
    assert mounts[0].mount_path == "/var/sdlcma/journal"

    env = {e.name: e.value for e in c.env}
    assert env["BF_JOURNAL_DIR"] == "/var/sdlcma/journal"


def test_spawn_with_both_host_aliases_and_journal_does_not_drop_either():
    """ls4900-shaped config: both options enabled. Belt-and-braces that the
    two features don't shadow each other in _build_job."""
    spawner, _, batch = _make_spawner(
        host_aliases=[{"ip": "100.64.0.5", "hostnames": ["minus"]}],
        journal_host_path="/var/sdlcma/journal",
    )
    asyncio.run(spawner.spawn("BUG-BOTH", "1", "u", "1"))
    pod = batch.create_namespaced_job.call_args.kwargs["body"].spec.template.spec
    assert pod.host_aliases is not None and len(pod.host_aliases) == 1
    assert pod.volumes is not None and len(pod.volumes) == 1
    env = {e.name: e.value for e in pod.containers[0].env}
    assert env["BF_JOURNAL_DIR"] == "/var/sdlcma/journal"


# ── W4: worker Job shaping ─────────────────────────────────────────────────
#
# resources / docker.sock / step-checkpoint hostPath / nodeSelector /
# tolerations / restart node affinity. Everything is default-empty, so the
# first test here is the one that matters most: with nothing configured the
# spec must still be the pre-W4 one. Design:
# /mnt/d/PL/sdlcma/W4-worker-job-design.md

def _pod_of(batch):
    return batch.create_namespaced_job.call_args.kwargs["body"].spec.template.spec


def _job_of(batch):
    return batch.create_namespaced_job.call_args.kwargs["body"]


# The seven env vars the pre-W4 spawner always set. Anything else appearing
# unconditionally would break the "empty config ⇒ unchanged spec" guarantee.
_BASE_ENV = {"BUG_ID", "project_id", "project_web_url", "job_id", "REDIS_URL",
             "BUG_SOURCE_BRANCH", "BF_CHECKPOINT_BACKEND"}

# Everything the spawner forwards from its OWN environment.
_FORWARDED = ("MINI_IMPL", "BF_STEP_CHECKPOINT", "BF_STEP_LEDGER",
              "BF_MINI_CONTAINER_TIMEOUT", "BF_MAX_COST_USD", "BF_AGENT_CONFIG")


@pytest.fixture
def clean_forwarded_env(monkeypatch):
    """Neutralise ambient forwarded vars for the "unchanged spec" assertions.

    Needed because the forwarding reads the PROCESS environment, and running
    the full suite mutates it: evaluation/runner.py sets
    `os.environ["BF_STEP_CHECKPOINT"] = "none"` process-wide to enforce
    invariant #4, and that outlives the test that triggered it. `"none"` is a
    truthy string, so the spawner forwards it — correct behaviour (an operator
    may well want to force it off for workers) but it means "which env vars
    does a default spawn emit" is only well-defined against a known
    environment. This test is about the W4 fields, not about that.
    """
    for var in _FORWARDED:
        monkeypatch.delenv(var, raising=False)


def test_w4_empty_config_leaves_spec_unchanged(clean_forwarded_env):
    """U1 — every W4 field unset ⇒ none of the new keys are rendered.

    labels are deliberately NOT compared here: D15 changed them on purpose
    (see test_w4_labels_*), and it is the ONE non-additive change in W4.
    """
    spawner, _, batch = _make_spawner()
    asyncio.run(spawner.spawn("BUG-W4-DEF", "1", "u", "1"))
    job = _job_of(batch)
    pod = job.spec.template.spec
    assert pod.node_selector is None
    assert pod.tolerations is None
    assert pod.affinity is None
    assert pod.volumes is None
    c = pod.containers[0]
    assert c.resources is None
    assert c.volume_mounts is None
    assert {e.name for e in c.env} == _BASE_ENV


def test_w4_cpu_request_only():
    """U3 — a single quantity renders requests and leaves limits absent."""
    spawner, _, batch = _make_spawner(cpu_request="3")
    asyncio.run(spawner.spawn("BUG-W4-CPU", "1", "u", "1"))
    res = _pod_of(batch).containers[0].resources
    assert res.requests == {"cpu": "3"}
    assert res.limits is None


def test_w4_all_quantities():
    """U4 — requests carry ephemeral-storage too (git clone + venv space)."""
    spawner, _, batch = _make_spawner(
        cpu_request="3", mem_request="4Gi", ephemeral_storage_request="20Gi",
        cpu_limit="8", mem_limit="8Gi",
    )
    asyncio.run(spawner.spawn("BUG-W4-ALL", "1", "u", "1"))
    res = _pod_of(batch).containers[0].resources
    assert res.requests == {"cpu": "3", "memory": "4Gi", "ephemeral-storage": "20Gi"}
    assert res.limits == {"cpu": "8", "memory": "8Gi"}


@pytest.mark.parametrize("bad", ["2 cores", "4 Gi", "", "3x", "-1", "Gi"])
def test_w4_invalid_quantity_is_fatal_at_construction(bad):
    """U5 — a typo must kill the orchestrator at startup, not every spawn.

    A bad quantity makes create_namespaced_job 422 forever, i.e. every bug
    silently dropped with the error buried in normal log traffic. Empty string
    means "unset" and is the one non-value that is allowed through.
    """
    if bad == "":
        spawner, _, _ = _make_spawner(cpu_request=bad)
        assert spawner._requests is None
        return
    with pytest.raises(ValueError, match="quantity"):
        _make_spawner(cpu_request=bad)


def test_w4_quantity_tolerates_surrounding_whitespace():
    """Values arrive via env vars and YAML, where a stray space is common and
    harmless — strip it rather than making the orchestrator refuse to start."""
    spawner, _, batch = _make_spawner(cpu_request="  3 ", mem_request="\t4Gi")
    asyncio.run(spawner.spawn("BUG-W4-WS", "1", "u", "1"))
    assert _pod_of(batch).containers[0].resources.requests == {
        "cpu": "3", "memory": "4Gi"}


def test_w4_limits_without_requests_warns_but_works(caplog):
    """U6 — legal in k8s (requests default to limits) but nearly always a typo."""
    with caplog.at_level("WARNING"):
        spawner, _, batch = _make_spawner(mem_limit="4Gi")
    asyncio.run(spawner.spawn("BUG-W4-LIM", "1", "u", "1"))
    res = _pod_of(batch).containers[0].resources
    assert res.limits == {"memory": "4Gi"} and res.requests is None
    assert any("limits set without requests" in r.message for r in caplog.records)


def test_w4_docker_sock_mounted_as_socket():
    """U7 — ver99 in a pod: mini shells out to `docker` against the node."""
    spawner, _, batch = _make_spawner(docker_sock="/var/run/docker.sock")
    asyncio.run(spawner.spawn("BUG-W4-SOCK", "1", "u", "1"))
    pod = _pod_of(batch)
    vol = [v for v in pod.volumes if v.name == "docker-sock"][0]
    assert vol.host_path.path == "/var/run/docker.sock"
    # `Socket`, not `File`: a mistyped path then fails at mount time with a
    # clear reason instead of silently handing the container an empty file.
    assert vol.host_path.type == "Socket"
    mount = [m for m in pod.containers[0].volume_mounts if m.name == "docker-sock"][0]
    assert mount.mount_path == "/var/run/docker.sock"


def test_w4_worker_container_is_never_downgraded_from_root():
    """U8 — the guard for D2.

    Mounting docker.sock only works because the container runs as root: the
    socket is root:docker 0660 and the docker GID differs per node (ls4900=137,
    minus=980, both measured), so no single supplementalGroups can be right on
    both. If anyone later adds runAsNonRoot/runAsUser "for hardening", ver99
    breaks with a permission-denied buried in mini's stderr. Fail here instead.
    """
    spawner, _, batch = _make_spawner(docker_sock="/var/run/docker.sock")
    asyncio.run(spawner.spawn("BUG-W4-ROOT", "1", "u", "1"))
    pod = _pod_of(batch)
    assert pod.security_context is None
    assert pod.containers[0].security_context is None


def test_w4_step_checkpoint_hostpath_and_env():
    """U9 — without this mount W2.5's resume does nothing at all on k8s:
    the records land in the Pod's own $HOME and die with the Pod."""
    spawner, _, batch = _make_spawner(
        step_checkpoint_host_path="/var/sdlcma/step_checkpoints")
    asyncio.run(spawner.spawn("BUG-W4-CKPT", "1", "u", "1"))
    pod = _pod_of(batch)
    vol = [v for v in pod.volumes if v.name == "step-checkpoint"][0]
    assert vol.host_path.path == "/var/sdlcma/step_checkpoints"
    assert vol.host_path.type == "DirectoryOrCreate"
    env = {e.name: e.value for e in pod.containers[0].env}
    assert env["BF_STEP_CHECKPOINT_DIR"] == "/var/sdlcma/step_checkpoints"


def test_w4_three_mounts_are_appended_in_order():
    """U10 — new volumes are APPENDED after the journal one.

    That ordering is what makes "empty config ⇒ byte-identical" a structural
    property rather than something to eyeball: nothing is ever inserted into
    the middle of an existing list.
    """
    spawner, _, batch = _make_spawner(
        journal_host_path="/var/sdlcma/journal",
        step_checkpoint_host_path="/var/sdlcma/step_checkpoints",
        docker_sock="/var/run/docker.sock",
    )
    asyncio.run(spawner.spawn("BUG-W4-3V", "1", "u", "1"))
    pod = _pod_of(batch)
    assert [v.name for v in pod.volumes] == ["journal", "step-checkpoint", "docker-sock"]
    assert [m.name for m in pod.containers[0].volume_mounts] == [
        "journal", "step-checkpoint", "docker-sock"]


def test_w4_step_checkpoint_dir_is_not_forwarded_from_the_orchestrator(monkeypatch):
    """U11 — BF_STEP_CHECKPOINT_DIR is owned by the mount, never forwarded.

    The orchestrator's own value names a path inside the ORCHESTRATOR's pod.
    Forwarding it would hand every worker a directory that doesn't exist there,
    and the step store is startup-strict, so every worker would die before its
    first LLM call. "Forward all BF_* vars" is the natural wrong instinct here.
    """
    monkeypatch.setenv("BF_STEP_CHECKPOINT_DIR", "/orchestrator/only")
    spawner, _, batch = _make_spawner(
        step_checkpoint_host_path="/var/sdlcma/step_checkpoints")
    asyncio.run(spawner.spawn("BUG-W4-NOFWD", "1", "u", "1"))
    env = [e for e in _pod_of(batch).containers[0].env
           if e.name == "BF_STEP_CHECKPOINT_DIR"]
    assert len(env) == 1
    assert env[0].value == "/var/sdlcma/step_checkpoints"


@pytest.mark.parametrize("var,value", [
    ("BF_STEP_LEDGER", "marker"),
    ("BF_MINI_CONTAINER_TIMEOUT", "4h"),
    ("BF_MAX_COST_USD", "5"),
])
def test_w4_new_env_forwarding(monkeypatch, clean_forwarded_env, var, value):
    """U12 — set ⇒ forwarded; unset ⇒ the key is absent entirely."""
    spawner, _, batch = _make_spawner()
    asyncio.run(spawner.spawn("BUG-W4-ENV0", "1", "u", "1"))
    assert var not in {e.name for e in _pod_of(batch).containers[0].env}

    monkeypatch.setenv(var, value)
    spawner, _, batch = _make_spawner()
    asyncio.run(spawner.spawn("BUG-W4-ENV1", "1", "u", "1"))
    env = {e.name: e.value for e in _pod_of(batch).containers[0].env}
    assert env[var] == value


def test_w4_node_selector_parsed():
    """U13"""
    spawner, _, batch = _make_spawner(
        node_selector='{"kubernetes.io/hostname":"ls4900"}')
    asyncio.run(spawner.spawn("BUG-W4-NS", "1", "u", "1"))
    assert _pod_of(batch).node_selector == {"kubernetes.io/hostname": "ls4900"}


@pytest.mark.parametrize("raw", ["{not json", '["a list"]', "null"])
def test_w4_bad_node_selector_is_logged_not_fatal(caplog, raw):
    """U14 — JSON settings stay forgiving, matching k8s_host_aliases.

    Fatal-on-typo is reserved for the resource quantities, where a mistake
    breaks every spawn instead of one optional feature.
    """
    with caplog.at_level("ERROR"):
        spawner, _, batch = _make_spawner(node_selector=raw)
    asyncio.run(spawner.spawn("BUG-W4-NSBAD", "1", "u", "1"))
    assert _pod_of(batch).node_selector is None
    if raw != "null":
        assert any("K8S_WORKER_NODE_SELECTOR" in r.message for r in caplog.records)


def test_w4_tolerations_parsed():
    """U15 — the two NoExecute entries that stop a cross-ocean hiccup from
    evicting a running ver99 worker at the k8s default of 300s."""
    spawner, _, batch = _make_spawner(tolerations=(
        '[{"key":"node.kubernetes.io/not-ready","operator":"Exists",'
        '"effect":"NoExecute","toleration_seconds":1800},'
        '{"key":"node.kubernetes.io/unreachable","operator":"Exists",'
        '"effect":"NoExecute","toleration_seconds":1800}]'))
    asyncio.run(spawner.spawn("BUG-W4-TOL", "1", "u", "1"))
    tols = _pod_of(batch).tolerations
    assert [t.key for t in tols] == ["node.kubernetes.io/not-ready",
                                     "node.kubernetes.io/unreachable"]
    assert all(t.toleration_seconds == 1800 for t in tols)
    assert all(t.effect == "NoExecute" for t in tols)


def test_w4_tolerations_accept_the_k8s_wire_shape():
    """Regression: the Helm overlay writes `tolerationSeconds` (camelCase),
    because that is what k8s docs, `kubectl -o yaml` and our values files all
    use — but V1Toleration only takes snake_case. Before this was handled,
    rendering the ver99 overlay produced a config that looked exactly right and
    made EVERY spawn die with a TypeError. Caught by diffing a real
    `helm template` against the spawner, not by a unit test — hence this one."""
    spawner, _, batch = _make_spawner(tolerations=(
        '[{"key":"node.kubernetes.io/unreachable","operator":"Exists",'
        '"effect":"NoExecute","tolerationSeconds":1800}]'))
    asyncio.run(spawner.spawn("BUG-W4-CAMEL", "1", "u", "1"))
    tol = _pod_of(batch).tolerations[0]
    assert tol.toleration_seconds == 1800
    assert tol.key == "node.kubernetes.io/unreachable"


def test_w4_unknown_toleration_field_is_dropped_not_fatal(caplog):
    """One typo in an optional field must not stop every worker from spawning."""
    with caplog.at_level("WARNING"):
        spawner, _, batch = _make_spawner(
            tolerations='[{"key":"x","operator":"Exists","tolerateSeconds":5}]')
        asyncio.run(spawner.spawn("BUG-W4-TYPO", "1", "u", "1"))
    tol = _pod_of(batch).tolerations[0]
    assert tol.key == "x" and tol.toleration_seconds is None
    assert any("tolerateSeconds" in r.message for r in caplog.records)


def test_w4_non_list_tolerations_ignored(caplog):
    """U16"""
    with caplog.at_level("ERROR"):
        spawner, _, batch = _make_spawner(tolerations='{"key":"x"}')
    asyncio.run(spawner.spawn("BUG-W4-TOLBAD", "1", "u", "1"))
    assert _pod_of(batch).tolerations is None
    assert any("K8S_WORKER_TOLERATIONS" in r.message for r in caplog.records)


def test_w4_invalid_resume_affinity_is_fatal():
    """U17 — a typo would degrade silently to "no affinity", which turns
    W2.5's exactly-once into a full re-run on every restart, unlogged."""
    with pytest.raises(ValueError, match="RESUME_AFFINITY"):
        _make_spawner(resume_affinity="prefered")


def test_w4_cold_spawn_never_gets_affinity(monkeypatch):
    """U18 — affinity is a RESTART concept; a first spawn has no node to
    prefer, so the spec must stay unchanged even with resume enabled."""
    monkeypatch.setenv("BF_STEP_CHECKPOINT", "file")
    spawner, _, batch = _make_spawner()
    asyncio.run(spawner.spawn("BUG-W4-COLD", "1", "u", "1"))
    assert _pod_of(batch).affinity is None


def _core_returning_node(node="minus"):
    core = MagicMock()
    core.list_namespaced_pod.return_value = SimpleNamespace(
        items=[SimpleNamespace(spec=SimpleNamespace(node_name=node))])
    return core


def test_w4_restart_prefers_the_previous_node(monkeypatch):
    """U19 — the whole point of D7: W2.5's evaluation container and its
    ledger are node-local, so a restart that lands elsewhere loses the resume."""
    monkeypatch.setenv("BF_STEP_CHECKPOINT", "file")
    core = _core_returning_node("minus")
    spawner, _, batch = _make_spawner(core=core)
    asyncio.run(spawner.spawn("BUG-W4-AFF", "1", "u", "1"))
    asyncio.run(spawner.restart("BUG-W4-AFF", "1", "u", "1"))

    aff = _pod_of(batch).affinity
    pref = aff.node_affinity.preferred_during_scheduling_ignored_during_execution
    assert len(pref) == 1 and pref[0].weight == 100
    expr = pref[0].preference.match_expressions[0]
    assert (expr.key, expr.operator, expr.values) == (
        "kubernetes.io/hostname", "In", ["minus"])
    # soft, not hard: a dead node must still schedule (and cold-start) rather
    # than leave the pod Pending forever with backoffLimit=0 hiding it
    assert aff.node_affinity.required_during_scheduling_ignored_during_execution is None


def test_w4_restart_without_resume_enabled_has_no_affinity(monkeypatch):
    """U20 — pinning a non-resumable worker to a node buys nothing and would
    make the spec differ for every deployment that isn't running ver99."""
    monkeypatch.delenv("BF_STEP_CHECKPOINT", raising=False)
    spawner, _, batch = _make_spawner(core=_core_returning_node("minus"))
    asyncio.run(spawner.spawn("BUG-W4-NOAFF", "1", "u", "1"))
    asyncio.run(spawner.restart("BUG-W4-NOAFF", "1", "u", "1"))
    assert _pod_of(batch).affinity is None


def test_w4_required_affinity_mode(monkeypatch):
    """U21 — the opt-in hard pin (single-node experiments only)."""
    monkeypatch.setenv("BF_STEP_CHECKPOINT", "file")
    spawner, _, batch = _make_spawner(core=_core_returning_node("ls4900"),
                                      resume_affinity="required")
    asyncio.run(spawner.spawn("BUG-W4-REQ", "1", "u", "1"))
    asyncio.run(spawner.restart("BUG-W4-REQ", "1", "u", "1"))
    na = _pod_of(batch).affinity.node_affinity
    assert na.preferred_during_scheduling_ignored_during_execution is None
    terms = na.required_during_scheduling_ignored_during_execution.node_selector_terms
    assert terms[0].match_expressions[0].values == ["ls4900"]


def test_w4_restart_reads_the_node_before_deleting_the_job(monkeypatch):
    """U22 — ORDER, not just presence.

    terminate() deletes the Job with Background propagation, so its pod
    disappears asynchronously. Reading nodeName after that returns nothing and
    the restart silently loses its node preference.
    """
    monkeypatch.setenv("BF_STEP_CHECKPOINT", "file")
    core = _core_returning_node("minus")
    batch = MagicMock()
    manager = MagicMock()
    manager.attach_mock(core, "core")
    manager.attach_mock(batch, "batch")

    spawner, _, _ = _make_spawner(batch=batch, core=core)
    asyncio.run(spawner.spawn("BUG-W4-ORDER", "1", "u", "1"))
    manager.reset_mock()
    asyncio.run(spawner.restart("BUG-W4-ORDER", "1", "u", "1"))

    names = [c[0] for c in manager.mock_calls]
    listed = names.index("core.list_namespaced_pod")
    deleted = names.index("batch.delete_namespaced_job")
    assert listed < deleted, f"node must be read before the Job is deleted: {names}"


def test_w4_node_lookup_failure_does_not_break_status():
    """U23 — reload_status() runs every HealthMonitor tick; a 403 or an API
    hiccup on the pod lookup must not disturb exit-code detection, which
    matters far more than the affinity hint it feeds."""
    batch = MagicMock()
    batch.read_namespaced_job_status.return_value = SimpleNamespace(
        status=SimpleNamespace(succeeded=None, failed=None, active=1))
    core = MagicMock()
    core.list_namespaced_pod.side_effect = RuntimeError("boom")
    proxy = K8sJobProxy(batch, "bf-worker-x", NS, core)
    proxy.reload_status()
    assert proxy.returncode is None
    assert proxy.node_name == ""


def test_w4_node_lookup_is_capped():
    """U24 — a Job whose pod never schedules would otherwise cost one pod LIST
    per tick forever."""
    core = MagicMock()
    core.list_namespaced_pod.return_value = SimpleNamespace(items=[])
    proxy = K8sJobProxy(MagicMock(), "bf-worker-pending", NS, core)
    for _ in range(30):
        proxy.lookup_node()
    assert core.list_namespaced_pod.call_count == K8sJobProxy._MAX_NODE_LOOKUPS


def test_w4_node_lookup_stops_after_success():
    """U25 — nodeName is immutable once scheduled; ask once."""
    core = _core_returning_node("ls4900")
    proxy = K8sJobProxy(MagicMock(), "bf-worker-x", NS, core)
    assert proxy.lookup_node() == "ls4900"
    proxy.lookup_node()
    assert core.list_namespaced_pod.call_count == 1


def test_w4_single_restart_owner_contract():
    """U26/U27 — backoffLimit=0 + restartPolicy=Never are a CONTRACT, not a
    conservative default: they declare the HealthMonitor as the single owner
    of restart policy.

    Hand any of it back to the Job controller and there are two owners: during
    a kubelet CrashLoopBackOff (10s→20s→40s…) the heartbeat key expires at 60s,
    the monitor spawns a replacement Job, and the backed-off pod then starts
    too. For ver99 that is two processes `docker exec`-ing into the SAME
    evaluation container, sharing one /.sdlcma ledger — W2.5's exactly-once
    breaks silently. Changing either value must fail here.
    """
    spawner, _, batch = _make_spawner(cpu_request="3",
                                      docker_sock="/var/run/docker.sock")
    asyncio.run(spawner.spawn("BUG-W4-OWN", "1", "u", "1"))
    job = _job_of(batch)
    assert job.spec.backoff_limit == 0
    assert job.spec.template.spec.restart_policy == "Never"


@pytest.mark.parametrize("bug_id", [
    "2026_08_03-11_22_33_4_ab12",   # the real orchestrator shape
    "astropy__astropy-12907",       # ver99: bug_id == instance_id
    "BUG-LOCAL-1",
])
def test_w4_labels_carry_the_real_bug_id(bug_id):
    """U33 — D15. Until W4 the `bug-id` label held the JOB NAME.

    All three of these were accepted verbatim by a live 1.36 apiserver (W4 L0),
    so no sanitising happens: a label value's alphabet is laxer than a
    DNS-1123 object name's — underscores and case are fine.
    """
    spawner, _, batch = _make_spawner()
    asyncio.run(spawner.spawn(bug_id, "1", "u", "1"))
    job = _job_of(batch)
    expected = {
        "app": "bf-worker",
        "job-name": _k8s_job_name(bug_id),
        "bug-id": bug_id,
    }
    assert job.metadata.labels == expected
    assert job.spec.template.metadata.labels == expected


def test_w4_label_value_is_sanitised_but_env_stays_authoritative():
    """U34 — a bug_id that is not a legal label value still produces a legal
    label, and the UNMODIFIED id remains in BUG_ID. Never reverse a sanitised
    label back into an identity."""
    weird = "a/b c!"
    spawner, _, batch = _make_spawner()
    asyncio.run(spawner.spawn(weird, "1", "u", "1"))
    job = _job_of(batch)
    label = job.metadata.labels["bug-id"]
    import re as _re
    assert _re.match(r"^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$", label), label
    assert len(label) <= 63
    env = {e.name: e.value for e in job.spec.template.spec.containers[0].env}
    assert env["BUG_ID"] == weird
