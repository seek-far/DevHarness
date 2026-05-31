"""Lint-style guards on infra/k8s/setup.sh.

These don't exercise the script — that needs a kind cluster + docker. They
just assert load-bearing invariants stay in place, so an unrelated cleanup
doesn't silently regress the live-fire smoke test.
"""

from __future__ import annotations

from pathlib import Path

SETUP_SH = Path(__file__).resolve().parents[1] / "infra" / "k8s" / "setup.sh"


def _setup_sh() -> str:
    return SETUP_SH.read_text()


def test_ingress_controller_pinned_to_control_plane():
    """ingress-nginx controller must be pinned to the control-plane node.

    kind cluster.yaml maps host :18080 → control-plane :80 via
    extraPortMappings, and the controller uses hostPort 80. If the controller
    lands on the worker node, host :18080 connects but immediately RSTs.
    The upstream provider/kind manifest historically pinned this via
    nodeSelector ingress-ready=true + control-plane toleration but `main`
    has drifted at least once (2026-05-30 incident), so setup.sh re-applies
    the invariant after `kubectl apply` regardless of what the manifest says.
    """
    content = _setup_sh()
    # Both pieces of the strategic patch must be present.
    assert 'ingress-ready' in content and '"true"' in content, (
        "setup.sh must re-pin ingress-nginx controller to ingress-ready=true "
        "after applying the upstream manifest"
    )
    assert 'node-role.kubernetes.io/control-plane' in content, (
        "setup.sh must add a control-plane toleration to the ingress-nginx "
        "controller so it can actually schedule there"
    )
    # The patch itself — guards against the toleration/label landing somewhere
    # unrelated (e.g. a comment) and the strategic merge being dropped.
    assert 'patch deploy ingress-nginx-controller' in content, (
        "setup.sh must `kubectl patch deploy ingress-nginx-controller` to "
        "enforce the nodeSelector + toleration"
    )


def test_grafana_url_detects_ingress():
    """The READY summary must report the actual Grafana surface, not assume one.

    The tracked chart ships NO grafana Ingress (→ port-forward localhost:3000),
    but values.local.yaml can add a sub-path Ingress sharing ingress-nginx
    :18080 (e.g. /grafana). Hardcoding either is wrong for the other host, so
    setup.sh queries the cluster for a `${RELEASE}-grafana` Ingress and prints
    the :18080 path when present, falling back to port-forward otherwise.
    """
    content = _setup_sh()
    assert 'get ingress "${RELEASE}-grafana"' in content, (
        "setup.sh must detect a grafana Ingress rather than assuming the "
        "port-forward path"
    )
    # Both branches must exist: the ingress :18080 path and the port-forward
    # fallback for the tracked default.
    assert 'http://localhost:18080${G_PATH}' in content, (
        "setup.sh must print the grafana ingress URL on :18080 when the "
        "Ingress exists"
    )
    assert 'port-forward svc/${RELEASE}-grafana 3000:80' in content, (
        "setup.sh must keep the port-forward fallback for the tracked chart "
        "(no grafana Ingress)"
    )
