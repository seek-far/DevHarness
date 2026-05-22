#!/usr/bin/env bash
# ============================================================================
# infra/k8s/setup.sh — bring up the SDLCMA stack on kind, wired to gitlab.com.
#
# Idempotent: re-runs without harm. Steps:
#   1 deps + token check
#   2 kind cluster (sdlcma-dev) — create if missing
#   3 build 3 images (dh-gateway / dh-orchestrator / dh-bf-worker)
#   4 kind load each (imagePullPolicy=IfNotPresent → no registry round-trip)
#   5 pre-create namespace + sdlcma-secrets (from settings/worker_gitlab_saas.env)
#   6 helm upgrade --install with values-gitlab-saas.yaml overlay
#   7 wait for rollouts (gateway / orchestrator / redis / cloudflared)
#   8 extract trycloudflare URL and print webhook target
#
# Env overrides (defaults shown):
#   CLUSTER_NAME=sdlcma-dev
#   NAMESPACE=sdlcma
#   RELEASE=sdlcma
#   CHART_DIR=infra/helm/sdlcma
#   VALUES_FILE=infra/helm/sdlcma/values-gitlab-saas.yaml
#   ENV_FILE=settings/worker_gitlab_saas.env
#
# Exit codes: 0 ready / 4 pre-flight fail
# ============================================================================
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

CLUSTER_NAME="${CLUSTER_NAME:-sdlcma-dev}"
NAMESPACE="${NAMESPACE:-sdlcma}"
RELEASE="${RELEASE:-sdlcma}"
CHART_DIR="${CHART_DIR:-infra/helm/sdlcma}"
VALUES_FILE="${VALUES_FILE:-infra/helm/sdlcma/values-gitlab-saas.yaml}"
ENV_FILE="${ENV_FILE:-settings/worker_gitlab_saas.env}"
KIND_CONFIG="${KIND_CONFIG:-infra/kind/cluster.yaml}"
IMAGES=(dh-gateway dh-orchestrator dh-bf-worker)

START_TS=$(date +%s)
say()   { printf '[t+%4ds] %s\n' $(($(date +%s) - START_TS)) "$*"; }
abort() { echo; echo "ABORT: $1" >&2; exit "${2:-4}"; }

# ── 1: deps + token check ──────────────────────────────────────────────────
say "1: env check"
for c in docker kind helm kubectl; do
  command -v "$c" >/dev/null || abort "missing dep: $c"
done
docker info >/dev/null 2>&1 || abort "docker daemon unreachable (start Docker Desktop?)"
[ -f "$ENV_FILE" ] || abort "missing $ENV_FILE (cp settings/worker_gitlab_saas.env.example and fill in)"
TOK=$(grep -oE '^GITLAB_PRIVATE_TOKEN=.+' "$ENV_FILE" | cut -d= -f2- | tr -d '"')
KEY=$(grep -oE '^LLM_API_KEY=.+'          "$ENV_FILE" | cut -d= -f2- | tr -d '"')
[ -n "$TOK" ] || abort "GITLAB_PRIVATE_TOKEN missing from $ENV_FILE"
[ -n "$KEY" ] || abort "LLM_API_KEY missing from $ENV_FILE"
say "  ✓ deps + creds"

# ── 2: kind cluster ────────────────────────────────────────────────────────
say "2: kind cluster $CLUSTER_NAME"
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  say "  ✓ cluster exists, reusing"
else
  kind create cluster --config "$KIND_CONFIG" 2>&1 | tail -10 | sed 's/^/  /'
  say "  ✓ created"
fi
kubectl config use-context "kind-$CLUSTER_NAME" >/dev/null
kubectl cluster-info --context "kind-$CLUSTER_NAME" >/dev/null \
  || abort "cluster context broken"

# ── 3: build images ────────────────────────────────────────────────────────
say "3: build images"
for img in "${IMAGES[@]}"; do
  case "$img" in
    dh-gateway)      DF=Dockerfile.gateway      ;;
    dh-orchestrator) DF=Dockerfile.orchestrator ;;
    dh-bf-worker)    DF=Dockerfile.bf-worker    ;;
  esac
  docker build -f "$DF" -t "$img:latest" . 2>&1 \
    | grep -E "^Step|writing image|naming" | tail -3 | sed 's/^/  /'
  say "  ✓ $img"
done

# ── 4: kind load ───────────────────────────────────────────────────────────
say "4: kind load"
for img in "${IMAGES[@]}"; do
  kind load docker-image "$img:latest" --name "$CLUSTER_NAME" 2>&1 \
    | tail -2 | sed 's/^/  /'
done

# ── 5: namespace + secret ──────────────────────────────────────────────────
say "5: namespace + sdlcma-secrets"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
# Delete-then-create makes secret rotation idempotent (Secret types can't be
# patched with --from-literal in a single command without recreate).
kubectl -n "$NAMESPACE" delete secret sdlcma-secrets --ignore-not-found >/dev/null
kubectl -n "$NAMESPACE" create secret generic sdlcma-secrets \
  --from-literal="GITLAB_PRIVATE_TOKEN=$TOK" \
  --from-literal="LLM_API_KEY=$KEY" >/dev/null
say "  ✓ secret created (GITLAB_PRIVATE_TOKEN + LLM_API_KEY; no SSH key — gitlab.com is HTTPS+oauth2)"

# ── 6: helm upgrade --install ──────────────────────────────────────────────
say "6: helm upgrade --install $RELEASE"
helm upgrade --install "$RELEASE" "$CHART_DIR" \
  -n "$NAMESPACE" \
  -f "$VALUES_FILE" \
  --set namespace.create=false \
  --wait --timeout 3m 2>&1 | tail -5 | sed 's/^/  /'

# ── 7: rollouts ────────────────────────────────────────────────────────────
say "7: wait rollouts"
for d in gateway orchestrator redis cloudflared; do
  kubectl -n "$NAMESPACE" rollout status "deploy/$d" --timeout=90s 2>&1 \
    | sed 's/^/  /'
done

# ── 8: cloudflared URL ─────────────────────────────────────────────────────
say "8: extract trycloudflare URL"
URL=""
for i in $(seq 1 30); do  # ~2.5 min
  URL=$(kubectl -n "$NAMESPACE" logs deploy/cloudflared --tail=200 2>/dev/null \
    | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | sort -u | tail -1)
  [ -n "$URL" ] && break
  sleep 5
done
[ -n "$URL" ] || abort "cloudflared URL not visible after 2.5 min (kubectl logs deploy/cloudflared)" 4
say "  ✓ tunnel URL: $URL"

echo
echo "=== READY ==="
echo "  cluster:     kind-$CLUSTER_NAME"
echo "  namespace:   $NAMESPACE"
echo "  webhook URL: ${URL}/webhook"
echo
echo "Next:"
echo "  1. Configure the gitlab.com project webhook to ${URL}/webhook (Pipeline + Job events)."
echo "  2. Trigger a failing CI on the test project, or run:"
echo "       PROJECT_PATH=user/repo bash infra/k8s/gitlab-smoke.sh"
echo "  3. Teardown: bash infra/k8s/teardown.sh"
