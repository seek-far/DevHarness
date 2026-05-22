#!/usr/bin/env bash
# ============================================================================
# infra/k8s/teardown.sh — tear down the kind-based SDLCMA stack.
#
# Default: helm uninstall + kind delete cluster (full clean).
# Use --keep-cluster to retain the kind cluster (e.g. for fast re-install).
# Use --keep-secret to retain sdlcma-secrets in the namespace.
#
# Env: CLUSTER_NAME=sdlcma-dev  NAMESPACE=sdlcma  RELEASE=sdlcma
# ============================================================================
set -uo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-sdlcma-dev}"
NAMESPACE="${NAMESPACE:-sdlcma}"
RELEASE="${RELEASE:-sdlcma}"

KEEP_CLUSTER=0
KEEP_SECRET=0
while [ $# -gt 0 ]; do
  case "$1" in
    --keep-cluster) KEEP_CLUSTER=1; shift ;;
    --keep-secret)  KEEP_SECRET=1;  shift ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

say() { printf '=== %s ===\n' "$*"; }

if kubectl config get-contexts -o name 2>/dev/null | grep -qx "kind-$CLUSTER_NAME"; then
  kubectl config use-context "kind-$CLUSTER_NAME" >/dev/null 2>&1 || true
  say "helm uninstall $RELEASE"
  helm uninstall "$RELEASE" -n "$NAMESPACE" --ignore-not-found 2>&1 | sed 's/^/  /' || true

  if [ "$KEEP_SECRET" = 0 ]; then
    kubectl -n "$NAMESPACE" delete secret sdlcma-secrets --ignore-not-found 2>&1 | sed 's/^/  /' || true
  fi

  # Drain leftover bf-worker Jobs (TTL handles successful ones; this catches
  # any still-running or failed ones the operator wants gone).
  kubectl -n "$NAMESPACE" delete jobs -l app=bf-worker --ignore-not-found 2>&1 | sed 's/^/  /' || true
fi

if [ "$KEEP_CLUSTER" = 0 ]; then
  say "kind delete cluster $CLUSTER_NAME"
  kind delete cluster --name "$CLUSTER_NAME" 2>&1 | sed 's/^/  /' || true
else
  say "--keep-cluster: kind cluster $CLUSTER_NAME retained"
fi

echo "done."
