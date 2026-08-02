"""Lint-style guards on infra/k3s/*.sh.

These don't exercise the scripts — that needs a live two-node k3s cluster
across two continents. They assert the load-bearing invariants stay in place,
each of which was a real hazard rather than a style preference. The kind
sibling is tests/test_k8s_setup_sh.py; the two harnesses coexist and must not
grow into each other.
"""

from __future__ import annotations

from pathlib import Path

import pytest

K3S = Path(__file__).resolve().parents[1] / "infra" / "k3s"

SETUP = K3S / "setup.sh"
TEARDOWN = K3S / "teardown.sh"
LOAD_IMAGE = K3S / "load-image.sh"
CROSSNODE = K3S / "crossnode-check.sh"
SMOKE = K3S / "gitlab-smoke.sh"

ALL_SCRIPTS = [SETUP, TEARDOWN, LOAD_IMAGE, CROSSNODE, SMOKE]


def _read(p: Path) -> str:
    return p.read_text()


def _code(p: Path) -> str:
    """Script text with comment lines stripped.

    Needed because these scripts document the traps they avoid, so the
    forbidden command appears verbatim in prose ("never run `kubectl config
    use-context`"). A grep over raw text would flag the explanation as the
    offence — and the natural "fix" is deleting the explanation.
    """
    return "\n".join(
        line for line in p.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


# Lines that only PRINT text (operator hints, next-step suggestions). They
# routinely name commands the script itself never runs, so a check for
# "does this script call kubectl?" has to exclude them or it fires on the
# help text.
_PRINTERS = ("say ", "echo ", "printf ", "cat <<")


def _invocations(p: Path) -> str:
    return "\n".join(
        line for line in _code(p).splitlines()
        if not line.lstrip().startswith(_PRINTERS)
    )


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda p: p.name)
def test_scripts_exist_and_are_bash(script: Path):
    assert script.exists(), f"{script} missing"
    assert _read(script).startswith("#!/usr/bin/env bash")


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda p: p.name)
def test_never_switches_the_global_kube_context(script: Path):
    """No script may run `kubectl config use-context`.

    Real incident: installing k3s copied its k3s.yaml over ~/.kube/config,
    deleting the kind context. infra/k8s/teardown.sh guards on that context
    existing, so on that host it silently became a no-op that exits 0 having
    torn down nothing. Two deployment shapes sharing one kubeconfig is how
    that happens. The k3s harness therefore uses a DEDICATED kubeconfig file
    and an explicit --context, and never mutates global kubectl state.
    """
    code = _code(script)
    assert "config use-context" not in code, (
        f"{script.name} switches the global kube context — use "
        "KUBECONFIG=$K3S_KUBECONFIG plus `kubectl --context` instead"
    )
    # Only scripts that actually talk to the API server need to resolve a
    # kubeconfig. load-image.sh drives docker + containerd and never calls
    # kubectl, so requiring it there would be cargo cult.
    if "kubectl" in _invocations(script):
        assert "K3S_KUBECONFIG" in code, (
            f"{script.name} calls kubectl but doesn't pin K3S_KUBECONFIG — it "
            "would inherit whatever ~/.kube/config happens to point at"
        )


def test_setup_taints_the_cross_continent_node():
    """setup.sh must taint the remote node BEFORE installing the chart.

    Worker Jobs carry no resources and no nodeSelector until plan item W4, so
    an untainted cross-continent node is an equally attractive scheduling
    target — a coin flip on every webhook. The taint is the declared default;
    W4 removes it together with the machinery that makes crossing safe.
    """
    content = _read(SETUP)
    assert "taint node" in content and "NoSchedule" in content
    assert "sdlcma.io/cross-continent" in content
    # Ordering: the taint must be applied before `helm upgrade --install`.
    assert content.index("taint node") < content.index("helm --kube-context"), (
        "the taint must be applied BEFORE the chart install, otherwise the "
        "first webhook can land a worker on the remote node"
    )


def test_teardown_keeps_the_taint_by_default():
    """Tearing down a release must not silently re-open cross-ocean scheduling.

    Removing the taint is W4's decision, made together with resources +
    toleration. An opt-in --untaint flag exists for when that is genuinely
    what you want.
    """
    content = _read(TEARDOWN)
    assert "--untaint" in content, "teardown must gate untainting behind a flag"
    assert "retained" in content, (
        "teardown must state that the taint is deliberately kept"
    )


def test_load_image_uses_the_k8s_io_containerd_namespace():
    """`ctr images import` without `-n k8s.io` lands in the wrong containerd
    namespace: `ctr images ls` shows the image while kubelet cannot find it,
    which surfaces as ImagePullBackOff with no obvious cause."""
    content = _read(LOAD_IMAGE)
    assert content.count("-n k8s.io") >= 2, (
        "every `k3s ctr images import` (local and remote) needs -n k8s.io"
    )
    assert "ctr -n k8s.io images import" in content


def test_load_image_has_a_no_credentials_path():
    """sudo is password-gated on BOTH nodes (measured), so a piped
    `ssh host 'sudo …'` hangs on an invisible password prompt when run
    unattended. The printed-command path is a first-class fallback, not a
    degraded one, and must exist."""
    content = _read(LOAD_IMAGE)
    assert "sudo -n true" in content, "must probe for passwordless sudo, not assume it"
    assert "K3S_MINUS_SSH" in content or "_SSH" in content
    assert "Manual step required" in content


def test_setup_checks_redis_isolation_against_the_rendered_values():
    """The host also runs the ver99 stack as plain subprocesses. Two
    orchestrators on one redis share `orchestrator-group` and its PEL, steal
    each other's pending entries, and spawn duplicate workers for one bug —
    silent corruption, not a crash.

    The check must read the RENDERED value: the overlay normally doesn't
    mention REDIS_URL at all (it inherits values.yaml), so grepping the
    overlay file would be a check that can only ever pass.
    """
    content = _read(SETUP)
    assert "helm template" in content, (
        "the redis-isolation check must render the merged values, not grep "
        "the overlay file (which usually doesn't set REDIS_URL at all)"
    )
    assert "redis://redis:" in content


def test_setup_needs_no_interactive_sudo():
    """setup.sh is routinely run over ssh / in the background, where a sudo
    password prompt hangs with no output. Anything needing root must print
    the command and exit 10 (distinct from 4 = pre-flight) instead."""
    content = _read(SETUP)
    assert "exit 10" in content
    assert "sudo install" in content, (
        "the kubeconfig bootstrap must be printed for the operator to run"
    )


def test_crossnode_check_does_not_handcraft_a_job_spec():
    """A6b must drive the REAL webhook → orchestrator → spawner chain.

    Hand-writing a Job would duplicate K8sJobSpawner._build_job's spec, and a
    hand-copied spec keeps passing after the real one changes — testing
    something that no longer exists. Constrain placement instead: drop the
    taint, cordon the server node, let the real chain place the worker.
    """
    content = _read(CROSSNODE)
    assert "kind: Job" not in content, "crossnode-check must not hand-write a Job spec"
    assert "cordon" in content and "taint node" in content
    assert "pipeline?ref=main" in content, (
        "the only way to spawn a worker is a failed-pipeline webhook"
    )


def test_crossnode_check_restores_node_state_and_verifies_it():
    """Leaving the server node cordoned would break every later deploy in a
    way that looks unrelated to this script. Issuing `uncordon` is not the
    same as it having taken effect, so the restore path reads the state back.
    """
    content = _read(CROSSNODE)
    assert "trap restore EXIT INT TERM" in content
    assert "RESTORE INCOMPLETE" in content, (
        "restore must verify by reading node state back, and say so loudly "
        "when it did not take"
    )


def test_crossnode_check_orders_diagnosis_before_blaming_the_network():
    """Every failure mode here looks identical from outside — 'no MR' — and
    the tempting story is always 'the ocean link is bad'. The script must
    check our own side first, in causal order."""
    content = _read(CROSSNODE)
    for probe in ("BugReportedEvent", "get pods -l app=bf-worker -o wide",
                  "describe pod"):
        assert probe in content, f"missing diagnostic step: {probe}"
    assert content.index("BugReportedEvent") < content.index("suspect the cross-ocean")


def test_crossnode_check_asserts_the_worker_actually_landed_remotely():
    """An MR proves the pipeline worked; it does not prove anything crossed
    an ocean. The node the worker pod ran on is the core assertion."""
    content = _read(CROSSNODE)
    assert 'WORKER_NODE" = "$REMOTE_NODE' in content or \
           '"$WORKER_NODE" = "$REMOTE_NODE"' in content
    assert "Nothing cross-ocean was proven" in content


def test_k3s_smoke_delegates_to_the_shared_script():
    """The acceptance criteria live in infra/k8s/gitlab-smoke.sh. A forked
    copy is a forked criterion, so the k3s wrapper only overrides endpoint /
    project / kubeconfig and execs the shared script."""
    content = _read(SMOKE)
    assert "exec bash infra/k8s/gitlab-smoke.sh" in content
    assert "GITLAB_API" in content


def test_shared_smoke_script_keeps_its_gitlab_com_default():
    """Parameterising GITLAB_API must not change the kind/gitlab.com path."""
    shared = (K3S.parent / "k8s" / "gitlab-smoke.sh").read_text()
    assert 'GITLAB_API="${GITLAB_API:-https://gitlab.com/api/v4}"' in shared


def test_k3s_uses_its_own_per_host_overlay_file():
    """The k3s harness must NOT auto-layer the kind harness's
    values.local.yaml.

    Found live on ls4900 (2026-08-02): that file is kind-era per-host state —
    a Grafana Ingress with ingressClassName: nginx and
    root_url http://localhost:18080/grafana. On the k3s cluster there is no
    ingress controller (traefik disabled) and :18080 died with the kind
    cluster, so layering it installs a resource nothing reconciles pointing at
    a URL that does not exist. It happened to be inert only because the k3s
    overlay ships monitoring.enabled=false — it would go live the moment
    monitoring is switched on.

    Same class of mistake as sharing ~/.kube/config across harnesses: per-host
    state must not be shared between deployment shapes.
    """
    code = _code(SETUP)
    assert "values.local-k3s.yaml" in code, (
        "setup.sh must default LOCAL_VALUES_FILE to a k3s-specific per-host "
        "overlay"
    )
    assert "values.local.yaml}" not in code, (
        "setup.sh must not default to the kind harness's values.local.yaml"
    )


def test_setup_waits_for_every_deployment_it_restarts():
    """Restarting two Deployments and waiting on one leaves the other
    mid-swap when the placement summary prints — two pods per Deployment,
    which reads like a scheduling bug that isn't there."""
    code = _code(SETUP)
    assert "for d in orchestrator gateway; do" in code, (
        "both restarted Deployments must be waited on before the summary"
    )


def test_setup_tolerates_an_unreachable_registry_for_third_party_images():
    """docker.io is routinely unreachable from the CN host while the daemon
    already has the image. A raw `docker pull` failure printed mid-run reads
    like a build failure; load-image.sh is the real guard (it inspects every
    image before importing)."""
    code = _code(SETUP)
    assert "docker image inspect redis:7-alpine" in code, (
        "a failed pull must fall back to checking the local daemon"
    )


def test_load_image_remote_default_is_worker_only():
    """A remote node only ever runs bf-worker Jobs.

    Every service Deployment is pinned to the server node by the overlay's
    nodeSelector, so defaulting the remote target to the full five-image set
    would push ~1 GB across an ocean for images nothing there can schedule.
    The local target still needs all of them.
    """
    code = _code(LOAD_IMAGE)
    assert "DEFAULT_REMOTE=(dh-bf-worker:latest)" in code, (
        "remote default must be bf-worker only"
    )
    assert "DEFAULT_LOCAL=" in code and "dh-gateway" in code, (
        "local default must still cover every service image"
    )
