#!/usr/bin/env bash
# ============================================================================
# infra/k3s/teardown.sh — remove the SDLCMA release from the k3s cluster.
#
# Scope on purpose: this uninstalls the RELEASE, not the cluster. k3s here is
# a long-lived systemd service shared with other work (and installed by hand,
# once). Wiping it is `sudo /usr/local/bin/k3s-uninstall.sh` — printed by
# --purge-cluster, never executed for you.
#
# Two things are deliberately NOT undone:
#   * the cross-continent taint on the remote node — removing it would
#     silently re-open cross-ocean scheduling for worker Jobs that still
#     have no resources/toleration. That is plan item W4's decision to make,
#     not a side effect of tearing down a release.
#   * images imported into containerd — they cost disk, not correctness, and
#     re-importing across an ocean is the expensive part of setup.
#
# Env: K3S_KUBECONFIG=~/.kube/k3s.yaml  KCTX=default
#      NAMESPACE=sdlcma  RELEASE=sdlcma  REMOTE_NODE=minus
# ============================================================================
set -uo pipefail

K3S_KUBECONFIG="${K3S_KUBECONFIG:-$HOME/.kube/k3s.yaml}"
KCTX="${KCTX:-default}"
NAMESPACE="${NAMESPACE:-sdlcma}"
RELEASE="${RELEASE:-sdlcma}"
REMOTE_NODE="${REMOTE_NODE:-minus}"
CROSS_TAINT_KEY="${CROSS_TAINT_KEY:-sdlcma.io/cross-continent}"

KEEP_SECRET=0
UNTAINT=0
PURGE_CLUSTER=0
while [ $# -gt 0 ]; do
  case "$1" in
    --keep-secret)   KEEP_SECRET=1;   shift ;;
    --untaint)       UNTAINT=1;       shift ;;
    --purge-cluster) PURGE_CLUSTER=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

say() { printf '=== %s ===\n' "$*"; }

[ -r "$K3S_KUBECONFIG" ] || { echo "no $K3S_KUBECONFIG — nothing to do"; exit 0; }
export KUBECONFIG="$K3S_KUBECONFIG"
kc() { kubectl --context "$KCTX" "$@"; }

say "helm uninstall $RELEASE"
helm --kube-context "$KCTX" uninstall "$RELEASE" -n "$NAMESPACE" --ignore-not-found 2>&1 | sed 's/^/  /' || true

if [ "$KEEP_SECRET" = 0 ]; then
  kc -n "$NAMESPACE" delete secret sdlcma-secrets --ignore-not-found 2>&1 | sed 's/^/  /' || true
fi

# TTL reaps finished Jobs; this catches ones still running or failed.
kc -n "$NAMESPACE" delete jobs -l app=bf-worker --ignore-not-found 2>&1 | sed 's/^/  /' || true

if [ "$UNTAINT" = 1 ]; then
  say "--untaint: removing $CROSS_TAINT_KEY from $REMOTE_NODE"
  echo "  NOTE: worker Jobs get no resources/nodeSelector/toleration until W4."
  echo "  Until then an untainted $REMOTE_NODE can absorb workers by coin flip."
  kc taint node "$REMOTE_NODE" "${CROSS_TAINT_KEY}-" 2>&1 | sed 's/^/  /' || true
else
  say "taint on $REMOTE_NODE retained (use --untaint to drop it)"
fi

if [ "$PURGE_CLUSTER" = 1 ]; then
  cat <<EOF

!! Cluster removal is not automated — k3s is a shared, hand-installed
   systemd service. Run these yourself, on the right host:

     ls4900 (server): sudo /usr/local/bin/k3s-uninstall.sh
     minus  (agent):  sudo /usr/local/bin/k3s-agent-uninstall.sh
     both:            sudo rm -rf /etc/rancher/k3s
EOF
fi

echo "done."
