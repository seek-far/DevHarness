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
        # 1a.6: NetworkPolicy ×3 + Ingress on by default; eval Job is NOT
        # (eval.enabled=false) — see test_eval_job_off_by_default.
        "NetworkPolicy", "NetworkPolicy", "NetworkPolicy",
        "Ingress",
    ])
    # Secret is intentionally NOT in the chart (out-of-band).
    assert not _by_kind(docs, "Secret")
    assert not _by_kind(docs, "Job")  # eval Job off by default


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


# ── 1a.6: NetworkPolicy / Ingress / observability / eval ──────────

def test_networkpolicy_deny_default_and_targeted_allows(docs):
    deny = _named(docs, "NetworkPolicy", "default-deny-ingress")
    assert deny["spec"]["podSelector"] == {}
    assert deny["spec"]["policyTypes"] == ["Ingress"]
    assert "ingress" not in deny["spec"]  # no rules → deny all inbound

    gw = _named(docs, "NetworkPolicy", "allow-gateway-ingress")
    assert gw["spec"]["podSelector"]["matchLabels"] == {"app": "gateway"}
    assert gw["spec"]["ingress"][0]["ports"][0]["port"] == 8000

    rd = _named(docs, "NetworkPolicy", "allow-redis-from-app")
    assert rd["spec"]["podSelector"]["matchLabels"] == {"app": "redis"}
    expr = rd["spec"]["ingress"][0]["from"][0]["podSelector"]["matchExpressions"][0]
    assert expr["key"] == "app" and expr["operator"] == "In"
    assert set(expr["values"]) == {"gateway", "orchestrator", "bf-worker"}
    assert rd["spec"]["ingress"][0]["ports"][0]["port"] == 6379


def test_networkpolicy_toggle_off():
    out = _helm("template", "sdlcma", str(CHART),
                "--set", "networkPolicy.enabled=false")
    kinds = [d["kind"] for d in yaml.safe_load_all(out) if d]
    assert "NetworkPolicy" not in kinds


def test_ingress_routes_to_gateway(docs):
    ing = _named(docs, "Ingress", "gateway")
    assert ing["spec"]["ingressClassName"] == "nginx"
    rule = ing["spec"]["rules"][0]
    assert "host" not in rule  # empty host → no-host rule
    backend = rule["http"]["paths"][0]["backend"]["service"]
    assert backend["name"] == "gateway"
    assert backend["port"]["number"] == 8000

    out = _helm("template", "sdlcma", str(CHART),
                "--set", "ingress.enabled=false")
    assert "Ingress" not in [d["kind"] for d in yaml.safe_load_all(out) if d]


def test_gateway_prometheus_annotations(docs):
    dep = _named(docs, "Deployment", "gateway")
    ann = dep["spec"]["template"]["metadata"]["annotations"]
    assert ann["prometheus.io/scrape"] == "true"
    assert ann["prometheus.io/port"] == "8000"
    assert ann["prometheus.io/path"] == "/healthz"

    out = _helm("template", "sdlcma", str(CHART),
                "--set", "observability.prometheusAnnotations=false")
    dep2 = next(d for d in yaml.safe_load_all(out)
                if d and d["kind"] == "Deployment"
                and d["metadata"]["name"] == "gateway")
    assert "annotations" not in dep2["spec"]["template"]["metadata"]


def test_eval_job_off_by_default(docs):
    assert not _by_kind(docs, "Job")


def test_eval_indexed_job_when_enabled():
    out = _helm("template", "sdlcma", str(CHART),
                "--set", "eval.enabled=true")
    job = next(d for d in yaml.safe_load_all(out)
               if d and d["kind"] == "Job"
               and d["metadata"]["name"] == "sdlcma-eval")
    assert job["spec"]["completionMode"] == "Indexed"
    assert job["spec"]["completions"] == 4
    assert job["spec"]["parallelism"] == 2
    assert job["spec"]["backoffLimit"] == 0
    pod = job["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["automountServiceAccountToken"] is False
