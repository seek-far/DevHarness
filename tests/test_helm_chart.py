"""Render-validation for the 1a.4 Helm chart (infra/helm/sdlcma).

Strategy: shell out to the real `helm` binary (the chart IS templated YAML —
the only faithful test is to render it the way Helm will) and assert the
manifest set + the parity-critical fields against what the raw kind manifests
under infra/kind/manifests/ produce. Skips cleanly if `helm` is not on PATH so
the suite still runs in environments without it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML ships with the kubernetes dep
    yaml = None

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART = REPO_ROOT / "infra" / "helm" / "sdlcma"

pytestmark = [
    pytest.mark.skipif(shutil.which("helm") is None, reason="helm not on PATH"),
    pytest.mark.skipif(yaml is None, reason="PyYAML not available"),
]


def _helm(*args: str) -> str:
    return subprocess.run(
        ["helm", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture(scope="module")
def docs():
    out = _helm("template", "sdlcma", str(CHART))
    return [d for d in yaml.safe_load_all(out) if d]


def _by_kind(docs, kind):
    return [d for d in docs if d.get("kind") == kind]


def _named(docs, kind, name):
    for d in _by_kind(docs, kind):
        if d["metadata"]["name"] == name:
            return d
    raise AssertionError(f"{kind}/{name} not rendered")


def test_helm_lint_clean():
    # `helm lint` exits non-zero on [ERROR]; the icon [INFO] is benign.
    _helm("lint", str(CHART))


def test_full_resource_set_renders(docs):
    kinds = sorted(d["kind"] for d in docs)
    assert kinds == sorted([
        "Namespace",
        "ServiceAccount",
        "ConfigMap", "ConfigMap", "ConfigMap",
        "PersistentVolumeClaim", "PersistentVolumeClaim",
        "Role", "RoleBinding",
        "Service", "Service",
        "Deployment", "Deployment", "Deployment",
    ])
    # Secret is intentionally NOT in the chart (out-of-band).
    assert not _by_kind(docs, "Secret")


def test_all_objects_in_target_namespace(docs):
    for d in docs:
        if d["kind"] == "Namespace":
            assert d["metadata"]["name"] == "sdlcma"
        else:
            assert d["metadata"]["namespace"] == "sdlcma", d["kind"]


@pytest.mark.parametrize("name,keys", [
    ("gateway-config", {"ENV", "USE_REDIS", "REDIS_URL", "GATEWAY_STREAM"}),
    ("orchestrator-config", {
        "ENV", "REDIS_URL", "GATEWAY_STREAM", "GATEWAY_CONSUMER_GROUP",
        "GATEWAY_CONSUMER_NAME", "WORKER_HEARTBEAT_INTERVAL",
        "WORKER_HEARTBEAT_TTL", "HEALTH_CHECK_INTERVAL", "STREAM_BLOCK_MS",
        "STREAM_COUNT", "WORKER_IMAGE"}),
    ("worker-config", {
        "ENV", "REDIS_URL", "WORKER_HEARTBEAT_INTERVAL", "WORKER_HEARTBEAT_TTL",
        "STREAM_BLOCK_MS", "GITLAB_API", "GITLAB_USERNAME", "GITLAB_SSH_PORT",
        "LLM_API_BASE_URL", "LLM_MODEL"}),
])
def test_configmap_keys_match_raw_manifests(docs, name, keys):
    cm = _named(docs, "ConfigMap", name)
    assert set(cm["data"]) == keys
    assert cm["data"]["ENV"] == "local_k8s"


def test_orchestrator_sa_and_envfrom(docs):
    dep = _named(docs, "Deployment", "orchestrator")
    spec = dep["spec"]["template"]["spec"]
    assert spec["serviceAccountName"] == "orchestrator"
    c = spec["containers"][0]
    cms = {e["configMapRef"]["name"] for e in c["envFrom"] if "configMapRef" in e}
    secs = {e["secretRef"]["name"] for e in c["envFrom"] if "secretRef" in e}
    assert cms == {"orchestrator-config", "worker-config"}
    assert secs == {"sdlcma-secrets"}
    assert dep["spec"]["strategy"]["type"] == "Recreate"


def test_gateway_envfrom_and_probes(docs):
    dep = _named(docs, "Deployment", "gateway")
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert [e["configMapRef"]["name"] for e in c["envFrom"]] == ["gateway-config"]
    assert c["readinessProbe"]["httpGet"]["path"] == "/healthz"
    assert c["image"] == "dh-gateway:latest"


def test_rbac_is_least_privilege(docs):
    role = _named(docs, "Role", "orchestrator-job-manager")
    rules = {r["resources"][0]: set(r["verbs"]) for r in role["rules"]}
    assert rules["jobs"] == {"create", "get", "list", "watch", "delete"}
    assert rules["pods"] == {"get", "list", "watch"}
    assert rules["pods/log"] == {"get"}
    # No create/delete on pods or secrets — the boundary 1a.3 promised.
    assert "secrets" not in rules
    rb = _named(docs, "RoleBinding", "orchestrator-job-manager")
    assert rb["subjects"][0]["name"] == "orchestrator"
    assert rb["roleRef"]["kind"] == "Role"  # namespaced, not ClusterRole


def test_namespace_toggle_off():
    out = _helm("template", "sdlcma", str(CHART),
                "--set", "namespace.create=false")
    kinds = [d["kind"] for d in yaml.safe_load_all(out) if d]
    assert "Namespace" not in kinds


def test_image_tag_override():
    out = _helm("template", "sdlcma", str(CHART),
                "--set", "gateway.image.tag=abc123")
    dep = next(d for d in yaml.safe_load_all(out)
               if d and d["kind"] == "Deployment"
               and d["metadata"]["name"] == "gateway")
    assert dep["spec"]["template"]["spec"]["containers"][0]["image"] == "dh-gateway:abc123"
