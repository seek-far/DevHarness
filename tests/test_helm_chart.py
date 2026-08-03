"""Render-validation for the 1a.4 Helm chart (infra/helm/sdlcma).

Strategy: shell out to the real `helm` binary (the chart IS templated YAML —
the only faithful test is to render it the way Helm will) and assert the
manifest set + the parity-critical fields against what the raw kind manifests
under infra/kind/manifests/ produce. Skips cleanly if `helm` is not on PATH so
the suite still runs in environments without it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


def _template_or_skip(*extra: str) -> str:
    """`helm template` the chart, skipping if the kube-prometheus-stack
    dependency tarball isn't built (charts/ is gitignored — needs
    `helm dep update`). Lets monitoring-on render tests run where the dep
    exists and skip cleanly where it doesn't."""
    try:
        return _helm("template", "sdlcma", str(CHART), *extra)
    except subprocess.CalledProcessError as exc:
        err = exc.stderr or ""
        if "missing in charts" in err or "found in Chart.yaml" in err:
            pytest.skip("kube-prometheus-stack dependency not built (helm dep update)")
        raise


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
    # LLM_API_BASE_URL / LLM_MODEL intentionally NOT in this set as of
    # 2026-05-30. Both are image-baked in settings/worker_local_k8s.env
    # (and per-env siblings: worker_gitlab_saas.env, etc.) — keeping
    # them out of the ConfigMap default lets the llmGateway.enabled=true
    # path (configmap.yaml seeds LLM_API_BASE_URL=http://llm-gateway:9000/v1)
    # actually take effect, since ConfigMap → process env wins over the
    # baked .env. See infra/helm/sdlcma/values.yaml configMaps.worker
    # for the comment thread that explains this trade.
    ("worker-config", {
        "ENV", "REDIS_URL", "WORKER_HEARTBEAT_INTERVAL", "WORKER_HEARTBEAT_TTL",
        "STREAM_BLOCK_MS", "GITLAB_API", "GITLAB_USERNAME", "GITLAB_SSH_PORT"}),
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
    # The deny policy must be SCOPED to the first-party core app pods, NOT a
    # namespace-wide `podSelector: {}`. An empty selector blackholes the
    # observability stack + worker→llm-gateway (incident 2026-05-31).
    deny = _named(docs, "NetworkPolicy", "deny-core-app-ingress")
    assert deny["spec"]["podSelector"] != {}, (
        "deny policy must not be namespace-wide — that blackholes grafana / "
        "Prometheus scrape / worker→llm-gateway"
    )
    expr = deny["spec"]["podSelector"]["matchExpressions"][0]
    assert expr["key"] == "app" and expr["operator"] == "In"
    assert set(expr["values"]) == {"gateway", "orchestrator", "redis", "bf-worker"}
    assert deny["spec"]["policyTypes"] == ["Ingress"]
    assert "ingress" not in deny["spec"]  # no rules → deny all inbound to those

    gw = _named(docs, "NetworkPolicy", "allow-gateway-ingress")
    assert gw["spec"]["podSelector"]["matchLabels"] == {"app": "gateway"}
    assert gw["spec"]["ingress"][0]["ports"][0]["port"] == 8000

    rd = _named(docs, "NetworkPolicy", "allow-redis-from-app")
    assert rd["spec"]["podSelector"]["matchLabels"] == {"app": "redis"}
    expr = rd["spec"]["ingress"][0]["from"][0]["podSelector"]["matchExpressions"][0]
    assert expr["key"] == "app" and expr["operator"] == "In"
    assert set(expr["values"]) == {"gateway", "orchestrator", "bf-worker"}
    assert rd["spec"]["ingress"][0]["ports"][0]["port"] == 6379


def test_networkpolicy_does_not_blackhole_observability():
    """With monitoring on, the deny policy must leave grafana / llm-gateway /
    runrecord-exporter unselected (→ default-allow) and add the orchestrator
    metrics-scrape allow. Regression guard for the 2026-05-31 504 incident."""
    out = _template_or_skip(
        "--set", "monitoring.enabled=true",
        "--set", "llmGateway.enabled=true",
        "--set", "runrecordExporter.enabled=true",
        "--set", "journal.persistence.enabled=true",  # exporter precondition
    )
    docs = [d for d in yaml.safe_load_all(out) if d]
    nps = _by_kind(docs, "NetworkPolicy")

    # The deny policy selects ONLY the four core app labels — nothing in its
    # selector reaches grafana / llm-gateway / runrecord-exporter / prometheus.
    deny = _named(docs, "NetworkPolicy", "deny-core-app-ingress")
    locked = set(deny["spec"]["podSelector"]["matchExpressions"][0]["values"])
    for must_stay_open in ("grafana", "llm-gateway", "runrecord-exporter", "prometheus"):
        assert must_stay_open not in locked

    # Orchestrator metrics scrape is explicitly allowed (orchestrator IS in the
    # deny set, so it needs its own allow or Prometheus can't reach :9102).
    om = _named(docs, "NetworkPolicy", "allow-orchestrator-metrics")
    assert om["spec"]["podSelector"]["matchLabels"] == {"app": "orchestrator"}
    assert om["spec"]["ingress"][0]["ports"][0]["port"] == 9102


def test_prometheus_selectors_match_all_monitors():
    """kube-prometheus-stack must be told to scrape our ServiceMonitors.

    Empty `serviceMonitorSelector: {}` is necessary but NOT sufficient — the
    subchart treats it as falsy and, with serviceMonitorSelectorNilUsesHelmValues
    at its `true` default, falls back to `matchLabels: {release: <name>}`. Our
    ServiceMonitors carry no `release` label, so they'd be silently skipped
    (incident 2026-05-31: empty Grafana panels). The NilUsesHelmValues=false
    flags are the load-bearing half. Static values check (no subchart render
    needed). """
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    spec = values["kube-prometheus-stack"]["prometheus"]["prometheusSpec"]
    assert spec["serviceMonitorSelector"] == {}
    assert spec["serviceMonitorSelectorNilUsesHelmValues"] is False, (
        "without NilUsesHelmValues=false the empty serviceMonitorSelector "
        "falls back to a release-label filter that excludes SDLCMA's "
        "ServiceMonitors — Prometheus scrapes nothing"
    )


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
    # checksum/config is always present (unconditional) — see gateway.yaml header.
    assert "checksum/config" in ann

    out = _helm("template", "sdlcma", str(CHART),
                "--set", "observability.prometheusAnnotations=false")
    dep2 = next(d for d in yaml.safe_load_all(out)
                if d and d["kind"] == "Deployment"
                and d["metadata"]["name"] == "gateway")
    # With prom annotations off, only checksum/config remains; the three
    # prometheus.io/* keys must be gone.
    ann2 = dep2["spec"]["template"]["metadata"].get("annotations", {})
    assert "checksum/config" in ann2
    assert "prometheus.io/scrape" not in ann2
    assert "prometheus.io/port"   not in ann2
    assert "prometheus.io/path"   not in ann2


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


# ── W3: multi-node scheduling knobs (nodeSelector + NodePort) ───────────────
# These exist for the cross-continent k3s cluster (infra/k3s/, docs/k3s.md).
# The whole point is that they are ADDITIVE: unset, the chart must render
# exactly what it rendered before they existed.

K3S_VALUES = CHART / "values-k3s-ls4900.yaml"

# Every component the k3s overlay pins. redis / llm-gateway / runrecord-
# exporter own node-local state (local-path PVC, sqlite cache, hostPath
# journal); gateway / orchestrator are pinned so the whole control path stays
# on one side of the ocean.
_PINNED = ["gateway", "orchestrator", "redis", "llm-gateway", "runrecord-exporter"]


def test_default_render_has_no_scheduling_fields(docs):
    """Default values must render NO nodeSelector and NO nodePort.

    This is the machine-checkable form of the additive guarantee: the
    single-node kind path (infra/k8s/) must be byte-identical to what it was
    before the k3s work. A `{{- with }}` guard that accidentally becomes
    unconditional (e.g. someone gives the value a non-empty default) would
    silently change every existing deployment's pod spec.
    """
    for dep in _by_kind(docs, "Deployment"):
        pod = dep["spec"]["template"]["spec"]
        assert "nodeSelector" not in pod, (
            f"{dep['metadata']['name']} rendered a nodeSelector under default "
            "values — the k3s knobs must stay opt-in"
        )
    svc = _named(docs, "Service", "gateway")
    assert svc["spec"]["type"] == "ClusterIP"
    for port in svc["spec"]["ports"]:
        assert "nodePort" not in port, (
            "gateway Service rendered a nodePort under default values"
        )


def test_k3s_overlay_pins_every_stateful_component():
    """The k3s overlay must pin all five, by hostname.

    Missing any one is a silent multi-node hazard rather than an error:
    local-path PVCs bind to whichever node schedules first and then stay
    there forever, so an unpinned redis can migrate the coordination hot
    path across an ocean and nothing reports it.
    """
    out = _template_or_skip("-f", str(K3S_VALUES))
    deps = {d["metadata"]["name"]: d
            for d in yaml.safe_load_all(out)
            if d and d.get("kind") == "Deployment"}
    for name in _PINNED:
        assert name in deps, f"{name} not rendered by the k3s overlay"
        sel = deps[name]["spec"]["template"]["spec"].get("nodeSelector")
        assert sel == {"kubernetes.io/hostname": "ls4900"}, (
            f"{name} is not pinned to ls4900 (got {sel!r})"
        )


def test_k3s_overlay_exposes_gateway_via_nodeport():
    """k3s has no kind extraPortMappings and traefik/servicelb are disabled
    (:80 belongs to the co-tenant GitLab), so the webhook enters via a fixed
    NodePort. 30800 is inside the default 30000-32767 range — 18080 would
    require widening --service-node-port-range on the server."""
    out = _template_or_skip("-f", str(K3S_VALUES))
    svc = next(d for d in yaml.safe_load_all(out)
               if d and d.get("kind") == "Service"
               and d["metadata"]["name"] == "gateway")
    assert svc["spec"]["type"] == "NodePort"
    assert svc["spec"]["ports"][0]["nodePort"] == 30800


def test_k3s_overlay_has_no_ingress_and_no_cloudflared():
    """Both webhook paths from the kind overlay must be OFF here.

    An Ingress with no controller to reconcile it is worse than no Ingress:
    it reads as "configured" while silently routing nothing.
    """
    out = _template_or_skip("-f", str(K3S_VALUES))
    kinds = [d.get("kind") for d in yaml.safe_load_all(out) if d]
    assert "Ingress" not in kinds
    names = [d["metadata"]["name"] for d in yaml.safe_load_all(out)
             if d and d.get("kind") == "Deployment"]
    assert "cloudflared" not in names


def test_k3s_overlay_sets_worker_pip_index():
    """apply_change_and_test builds a CLEAN venv, so the fixture repo's
    requirements.txt (pytest) is pip-installed at run time. ls4900 is on a CN
    network and gitlab-runner's own PIP_INDEX_URL does NOT reach the worker,
    so the worker ConfigMap has to carry a mirror or every ver0 run dies in
    `pip install`. ver99 skips local pytest, which is why this path had never
    been exercised there."""
    out = _template_or_skip("-f", str(K3S_VALUES))
    cm = next(d for d in yaml.safe_load_all(out)
              if d and d.get("kind") == "ConfigMap"
              and d["metadata"]["name"] == "worker-config")
    assert cm["data"].get("PIP_INDEX_URL"), (
        "worker-config must carry PIP_INDEX_URL on the CN host"
    )


def test_k3s_overlay_keeps_spawner_decoupled_from_env():
    """Project invariant #1: never invent a new ENV for a spawner. The k3s
    overlay reuses ENV=local_multi_process (ls4900's own GitLab) and carries
    the spawner choice in WORKER_SPAWNER."""
    out = _template_or_skip("-f", str(K3S_VALUES))
    cm = next(d for d in yaml.safe_load_all(out)
              if d and d.get("kind") == "ConfigMap"
              and d["metadata"]["name"] == "orchestrator-config")
    assert cm["data"]["ENV"] == "local_multi_process"
    assert cm["data"]["WORKER_SPAWNER"] == "k8s"


def test_k3s_overlay_gateway_backend_uses_the_injected_key_name():
    """The gateway backend must read the key name the chart actually injects.

    The chart plumbs exactly one credential into the llm-gateway pod:
    LLM_API_KEY, from sdlcma-secrets (which setup.sh fills from the worker env
    file). A backend declaring any other api_key_env gets an unset variable;
    a backend pointing at a DIFFERENT PROVIDER than the key belongs to gets a
    401 from that provider.

    Both happened on the first A7 run: the chart default is qwen3-on-Dashscope
    while ls4900's key is DeepSeek's, so the gateway presented a DeepSeek key
    to Aliyun and the run died in react_loop with
    401 "Incorrect API key" — after fetch_trace and parse_trace had already
    succeeded, which is what makes it look like a code bug rather than a
    config mismatch.
    """
    out = _template_or_skip("-f", str(K3S_VALUES))
    cm = next(d for d in yaml.safe_load_all(out)
              if d and d.get("kind") == "ConfigMap"
              and d["metadata"]["name"] == "llm-gateway-config")
    cfg = yaml.safe_load(cm["data"]["config.yaml"])
    backends = cfg["backends"]
    assert backends, "no gateway backend configured"
    for b in backends:
        assert b["api_key_env"] == "LLM_API_KEY", (
            f"backend {b['name']} reads {b['api_key_env']}, but the chart only "
            "injects LLM_API_KEY into the gateway pod"
        )
    # The overlay is host-specific (it is named for the host), so it may and
    # should pin the provider that host's key belongs to.
    assert any("deepseek" in b["base_url"] for b in backends), (
        "ls4900's LLM_API_KEY is a DeepSeek credential — pointing the backend "
        "anywhere else reproduces the 401"
    )


# ── W4: worker Job shaping + the ver99 overlay ─────────────────────────────
#
# The pattern to notice here: the chart's job is to hand the ORCHESTRATOR a set
# of K8S_WORKER_* strings, and the orchestrator's job is to turn those into a
# Job spec. Testing each half alone leaves the seam between them untested —
# which is exactly where the first real W4 bug lived (Helm writes the k8s wire
# shape `tolerationSeconds`, V1Toleration takes `toleration_seconds`, so every
# spawn would have died on a config that reads perfectly). The last test in
# this block closes that seam by feeding the RENDERED values into the real
# spawner.

VER99_VALUES = CHART / "values-k3s-ver99.yaml"


def _orchestrator_config(out: str) -> dict:
    cm = next(d for d in yaml.safe_load_all(out)
              if d and d.get("kind") == "ConfigMap"
              and d["metadata"]["name"] == "orchestrator-config")
    return cm["data"]


def test_worker_job_defaults_emit_no_k8s_worker_keys(docs):
    """H2 — stock values ⇒ not one K8S_WORKER_* key ⇒ the spawner keeps its
    default-empty settings ⇒ the Job spec is the pre-W4 one."""
    cm = _named(docs, "ConfigMap", "orchestrator-config")
    leaked = [k for k in cm["data"] if k.startswith("K8S_WORKER_")]
    assert leaked == [], f"empty workerJob still emitted {leaked}"


def test_ver99_overlay_emits_the_worker_job_keys():
    """H3"""
    out = _template_or_skip("-f", str(K3S_VALUES), "-f", str(VER99_VALUES))
    data = _orchestrator_config(out)
    assert data["K8S_WORKER_CPU_REQUEST"] == "3"
    assert data["K8S_WORKER_MEM_REQUEST"] == "4Gi"
    assert data["K8S_WORKER_EPHEMERAL_STORAGE_REQUEST"] == "20Gi"
    # limits deliberately absent: they constrain only the worker process, while
    # the container that actually eats the node is a sibling on the host
    # dockerd, in another cgroup entirely.
    assert "K8S_WORKER_CPU_LIMIT" not in data
    assert "K8S_WORKER_MEM_LIMIT" not in data
    assert data["K8S_WORKER_DOCKER_SOCK"] == "/var/run/docker.sock"
    assert data["K8S_WORKER_STEP_CHECKPOINT_HOST_PATH"] == "/var/sdlcma/step_checkpoints"
    tols = json.loads(data["K8S_WORKER_TOLERATIONS"])
    # Exactly two: W4 removed the cross-continent taint rather than tolerating
    # it, so the only tolerations left are the eviction-delay pair.
    assert len(tols) == 2
    assert {t["key"] for t in tols} == {"node.kubernetes.io/not-ready",
                                        "node.kubernetes.io/unreachable"}
    assert all(t["tolerationSeconds"] == 1800 for t in tols)


def test_ver99_overlay_worker_image_is_immutable_tagged():
    """H4 — the point of this test is the NOT-`:latest` assertion.

    imagePullPolicy is IfNotPresent and images are side-loaded with
    `k3s ctr images import` — there is no registry to re-pull from. A moving
    tag therefore lets two nodes hold different content under one name, with
    nothing to reveal it.
    """
    out = _template_or_skip("-f", str(K3S_VALUES), "-f", str(VER99_VALUES))
    image = _orchestrator_config(out)["WORKER_IMAGE"]
    assert image.startswith("dh-bf-worker-swebench:")
    assert not image.endswith(":latest"), (
        "the ver99 overlay must reference an immutable tag from "
        "build-swebench-image.sh, never :latest"
    )


def test_ver99_overlay_sets_resume_env_on_the_orchestrator_side():
    """The spawner forwards these from its OWN process env, so putting them
    under configMaps.worker would look right and do nothing."""
    out = _template_or_skip("-f", str(K3S_VALUES), "-f", str(VER99_VALUES))
    data = _orchestrator_config(out)
    assert data["MINI_IMPL"] == "vendored"
    assert data["BF_STEP_CHECKPOINT"] == "file"
    assert data["BF_STEP_LEDGER"] == "ledger"
    # invariant #1: the spawner choice never rides on ENV
    assert data["ENV"] == "local_multi_process"
    assert data["WORKER_SPAWNER"] == "k8s"


def test_ver99_overlay_sets_agent_config_in_exactly_one_place():
    """An explicit container env beats envFrom, so BF_AGENT_CONFIG living in
    both the worker ConfigMap and the orchestrator's env is a silent-precedence
    trap. It belongs to the worker ConfigMap only."""
    out = _template_or_skip("-f", str(K3S_VALUES), "-f", str(VER99_VALUES))
    docs_ = [d for d in yaml.safe_load_all(out) if d]
    worker = _named(docs_, "ConfigMap", "worker-config")["data"]
    orch = _named(docs_, "ConfigMap", "orchestrator-config")
    assert worker["BF_AGENT_CONFIG"] == "/app/configs/swebench/gitlab_ver99.json"
    assert "BF_AGENT_CONFIG" not in orch["data"]


def test_ver99_overlay_keeps_the_k3s_pins():
    """H5 — layering the ver99 overlay must not undo values-k3s-ls4900's
    node pinning (helm merges maps, but a mistyped key silently replaces)."""
    out = _template_or_skip("-f", str(K3S_VALUES), "-f", str(VER99_VALUES))
    deps = {d["metadata"]["name"]: d for d in yaml.safe_load_all(out)
            if d and d.get("kind") == "Deployment"}
    for name in _PINNED:
        sel = deps[name]["spec"]["template"]["spec"].get("nodeSelector")
        assert sel == {"kubernetes.io/hostname": "ls4900"}, name


def _pvc_carrying_workloads(out: str):
    """Every rendered workload that mounts a PVC or declares one.

    Structural on purpose: it walks the manifests instead of naming the four
    components we happen to know about today, so a stateful component added
    later fails this test rather than failing an ocean away.
    """
    found = []
    for d in yaml.safe_load_all(out):
        if not d or d.get("kind") not in ("Deployment", "StatefulSet", "DaemonSet"):
            continue
        spec = d["spec"]["template"]["spec"]
        has_pvc = any("persistentVolumeClaim" in v for v in spec.get("volumes", []) or [])
        has_vct = bool(d["spec"].get("volumeClaimTemplates"))
        if has_pvc or has_vct:
            found.append((d["kind"], d["metadata"]["name"], spec.get("nodeSelector")))
    return found


def test_k3s_overlay_pins_every_pvc_carrying_workload():
    """H7 — the guard that replaces the taint.

    W4 removes the cross-continent NoSchedule taint so worker Jobs can run on
    the remote node. That taint was also the second line of defence keeping
    stateful pods on the server node; `nodeSelector` is now the only one.
    A local-path PVC that binds on the remote node pins its PV there
    permanently (the generated PV carries its own nodeAffinity), so recovering
    means deleting the PV and losing the data.

    Runs with monitoring ON because that is where the un-pinned components
    hide: Prometheus / Grafana / Alertmanager come from the sub-chart and are
    invisible in the default render.
    """
    out = _template_or_skip("-f", str(K3S_VALUES), "--set", "monitoring.enabled=true")
    workloads = _pvc_carrying_workloads(out)
    assert workloads, "expected at least one PVC-carrying workload with monitoring on"
    unpinned = [(k, n) for k, n, sel in workloads if not sel]
    assert not unpinned, (
        f"PVC-carrying workloads without a nodeSelector: {unpinned}. On this "
        "cluster that risks binding a node-local PV on the cross-continent "
        "node, which is irreversible. Pin them in values-k3s-ls4900.yaml."
    )


def test_k3s_overlay_pins_the_operator_managed_stateful_crs():
    """H7b — the half the walker above CANNOT see.

    Prometheus and Alertmanager are not rendered workloads: the chart emits
    CUSTOM RESOURCES and the Operator builds the StatefulSets at runtime. So
    the PVC walker returns nothing for them and would pass vacuously — the
    exact components D4.1 is about.

    They also carry no PVC in our current values (storage unset ⇒ emptyDir),
    which is precisely why nodeSelector must be asserted REGARDLESS of storage:
    turning persistence on later is a one-line values change that would
    silently reintroduce the irreversible-PV hazard.
    """
    out = _template_or_skip("-f", str(K3S_VALUES), "--set", "monitoring.enabled=true")
    crs = [d for d in yaml.safe_load_all(out)
           if d and d.get("kind") in ("Prometheus", "Alertmanager")]
    assert crs, (
        "no Prometheus/Alertmanager CR rendered with monitoring on — this test "
        "has stopped covering anything; check the sub-chart's shape"
    )
    unpinned = [(d["kind"], d["metadata"]["name"]) for d in crs
                if not d.get("spec", {}).get("nodeSelector")]
    assert not unpinned, (
        f"operator-managed stateful CRs without spec.nodeSelector: {unpinned}. "
        "Pin them under kube-prometheus-stack in values-k3s-ls4900.yaml."
    )


def test_default_render_has_no_pvc_workload_expectations(docs):
    """H8 — the guard above must not silently pass by finding nothing.

    With monitoring off (the default) the set is redis alone, and redis IS
    pinned by the k3s overlay; here we only assert the walker works.
    """
    out = _helm("template", "sdlcma", str(CHART))
    names = {n for _, n, _ in _pvc_carrying_workloads(out)}
    assert "redis" in names, (
        "the PVC walker found no redis — it has stopped detecting PVCs and "
        "test_k3s_overlay_pins_every_pvc_carrying_workload is now vacuous"
    )


def test_rendered_worker_job_values_survive_the_real_spawner():
    """The chart↔code seam, end to end.

    Renders the ver99 overlay, feeds the K8S_WORKER_* strings it produced into
    the real K8sJobSpawner, and asserts the Job it builds. This is the test
    that would have caught the tolerationSeconds/toleration_seconds mismatch:
    both halves were individually correct and the pair was broken.
    """
    from unittest.mock import MagicMock, patch
    import kubernetes
    from orchestrator.registry import WorkerRegistry
    from orchestrator.spawner import K8sJobSpawner

    out = _template_or_skip("-f", str(K3S_VALUES), "-f", str(VER99_VALUES))
    data = _orchestrator_config(out)

    with patch("kubernetes.config.load_incluster_config",
               side_effect=kubernetes.config.config_exception.ConfigException), \
         patch("kubernetes.config.load_kube_config"), \
         patch("kubernetes.client.BatchV1Api", return_value=MagicMock()), \
         patch("kubernetes.client.CoreV1Api", return_value=MagicMock()):
        spawner = K8sJobSpawner(
            registry=WorkerRegistry(), redis_url="redis://redis:6379/0",
            worker_image=data["WORKER_IMAGE"], namespace="sdlcma",
            worker_config_map="worker-config", secret_name="sdlcma-secrets",
            job_ttl_seconds=600,
            cpu_request=data.get("K8S_WORKER_CPU_REQUEST", ""),
            mem_request=data.get("K8S_WORKER_MEM_REQUEST", ""),
            cpu_limit=data.get("K8S_WORKER_CPU_LIMIT", ""),
            mem_limit=data.get("K8S_WORKER_MEM_LIMIT", ""),
            ephemeral_storage_request=data.get(
                "K8S_WORKER_EPHEMERAL_STORAGE_REQUEST", ""),
            docker_sock=data.get("K8S_WORKER_DOCKER_SOCK", ""),
            step_checkpoint_host_path=data.get(
                "K8S_WORKER_STEP_CHECKPOINT_HOST_PATH", ""),
            node_selector=data.get("K8S_WORKER_NODE_SELECTOR", ""),
            tolerations=data.get("K8S_WORKER_TOLERATIONS", ""),
            resume_affinity=data.get("K8S_WORKER_RESUME_AFFINITY", "preferred"),
        )
        job = spawner._build_job("bf-worker-x", "astropy__astropy-12907",
                                 "1", "http://gitlab/x.git", "9")

    pod = job.spec.template.spec
    c = pod.containers[0]
    assert c.resources.requests == {"cpu": "3", "memory": "4Gi",
                                    "ephemeral-storage": "20Gi"}
    assert [v.name for v in pod.volumes] == ["step-checkpoint", "docker-sock"]
    assert [t.toleration_seconds for t in pod.tolerations] == [1800, 1800]
    assert job.metadata.labels["bug-id"] == "astropy__astropy-12907"
