#!/usr/bin/env bash
# ============================================================================
# infra/k8s/setup.sh — bring up the SDLCMA stack on kind, wired to gitlab.com.
#
# Idempotent: re-runs without harm. Steps:
#   1  deps + token check
#   2  kind cluster (sdlcma-dev) — create if missing
#   3  build 3 images (dh-gateway / dh-orchestrator / dh-bf-worker)
#   4  kind load each (imagePullPolicy=IfNotPresent → no registry round-trip)
#   4b ingress-nginx (kind add-on, registry.k8s.io rewritten through
#      m.daocloud.io proxy for digest-pinned CN-reachable pull); skip with
#      INSTALL_INGRESS_NGINX=0
#   5  pre-create namespace + sdlcma-secrets (from settings/worker_gitlab_saas.env)
#   6  helm upgrade --install with values-gitlab-saas.yaml overlay
#   7  wait for rollouts (gateway / orchestrator / redis / cloudflared)
#   8  extract trycloudflare URL and print webhook targets (cloudflared +
#      ingress host/tailnet)
#
# Env overrides (defaults shown):
#   CLUSTER_NAME=sdlcma-dev
#   NAMESPACE=sdlcma
#   RELEASE=sdlcma
#   CHART_DIR=infra/helm/sdlcma
#   VALUES_FILE=infra/helm/sdlcma/values-gitlab-saas.yaml
#   ENV_FILE=settings/worker_gitlab_saas.env
#   INSTALL_INGRESS_NGINX=1                    (set 0 to skip step 4b)
#   INGRESS_NGINX_PROXY=m.daocloud.io/         (rewrite prefix; "" = upstream
#                                               registry.k8s.io for non-CN nets)
#   INGRESS_NGINX_MANIFEST=https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml
#   LOCAL_VALUES_FILE=infra/helm/sdlcma/values.local.yaml
#                                              (per-host helm overlay; auto-
#                                               layered on $VALUES_FILE when
#                                               present; gitignored. Typical
#                                               use: cloudflared.enabled:false
#                                               when ingress-nginx covers the
#                                               webhook ingress.)
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
# Default to the "ls4900 production-rehearsal" overlay: kind cluster on the
# tailnet, against the operator's self-hosted GitLab at host `minus`. Full
# observability stack on (kube-prometheus-stack + Grafana dashboard + LLM
# gateway with persistent cache + runrecord exporter). Override to point
# at gitlab.com or a different operator setup:
#   VALUES_FILE=infra/helm/sdlcma/values-gitlab-saas.yaml \
#   ENV_FILE=settings/worker_gitlab_saas.env \
#   bash infra/k8s/setup.sh
VALUES_FILE="${VALUES_FILE:-infra/helm/sdlcma/values-gitlab-minus.yaml}"
# Per-host overlay layered ON TOP of $VALUES_FILE when it exists. Gitignored
# (.gitignore: infra/helm/sdlcma/values.local*.yaml). Use it for host-
# specific divergences — e.g. `cloudflared.enabled: false` when the host has
# direct webhook reachability via the ingress-nginx path (tailnet/LAN/host).
LOCAL_VALUES_FILE="${LOCAL_VALUES_FILE:-infra/helm/sdlcma/values.local.yaml}"
ENV_FILE="${ENV_FILE:-settings/worker_local_multi_process.env.gitlab@minus}"
KIND_CONFIG="${KIND_CONFIG:-infra/kind/cluster.yaml}"
# dh-llm-gateway built+loaded unconditionally — kind load is cheap, and the
# chart only references the image when llmGateway.enabled=true (so disabled
# overlays don't touch it at runtime). Keeps setup.sh oblivious to whether
# the operator turned the gateway on or off.
IMAGES=(dh-gateway dh-orchestrator dh-bf-worker dh-llm-gateway)

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
    dh-llm-gateway)  DF=Dockerfile.llm-gateway  ;;
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

# Third-party images: kubelet inside the kind worker container has its own
# containerd that does NOT inherit the host docker daemon's registry-mirrors.
# On hosts where docker.io is unreachable directly (e.g. CN networks even with
# a working host-side mirror), the redis/cloudflared Deployments stall in
# ImagePullBackOff. Mirror the dh-* pattern: pull on the host (which CAN use
# the host's daemon.json mirror), then kind-load into both nodes.
THIRD_PARTY_IMAGES=(redis:7-alpine cloudflare/cloudflared:latest)
for img in "${THIRD_PARTY_IMAGES[@]}"; do
  docker pull "$img" 2>&1 | tail -1 | sed 's/^/  /'
  kind load docker-image "$img" --name "$CLUSTER_NAME" 2>&1 \
    | tail -2 | sed 's/^/  /'
done

# ── 4b: ingress-nginx (optional; tailnet/LAN/hostname webhook ingress) ─────
# The chart's Ingress resource (templates/ingress.yaml, ingress.enabled=true
# by default) routes :80 on the kind control-plane node → svc/gateway:8000.
# The kind cluster.yaml maps host :18080 → control-plane :80, so a working
# ingress-nginx controller closes the chain and exposes the webhook on the
# host's :18080 without cloudflared. Use this when GitLab can reach the
# agent host directly (tailnet, LAN, public hostname). cloudflared remains
# for no-direct-connectivity setups; both can coexist.
#
# CN-network gotcha: the upstream manifest pins controller + kube-webhook-
# certgen images by @sha256 digest at registry.k8s.io, so kind load + retag
# does NOT work — kubelet resolves the digest at the original URL. Rewrite
# the manifest through the m.daocloud.io transparent proxy (which preserves
# digests) before applying. Verified 2026-05-27 on ls4900 (CN, tailnet).
#
# Set INSTALL_INGRESS_NGINX=0 to skip (cloudflared-only path).
INSTALL_INGRESS_NGINX="${INSTALL_INGRESS_NGINX:-1}"
INGRESS_NGINX_MANIFEST="${INGRESS_NGINX_MANIFEST:-https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml}"
INGRESS_NGINX_PROXY="${INGRESS_NGINX_PROXY:-m.daocloud.io/}"
if [ "$INSTALL_INGRESS_NGINX" = "1" ]; then
  say "4b: ingress-nginx (kind add-on, via ${INGRESS_NGINX_PROXY}registry.k8s.io)"
  TMP_MF=$(mktemp -t ingress-nginx-XXXX.yaml)
  if ! curl -sSL "$INGRESS_NGINX_MANIFEST" \
      | sed "s|registry.k8s.io/|${INGRESS_NGINX_PROXY}registry.k8s.io/|g" \
      > "$TMP_MF"; then
    rm -f "$TMP_MF"
    abort "failed to download ingress-nginx manifest from $INGRESS_NGINX_MANIFEST"
  fi
  kubectl apply -f "$TMP_MF" 2>&1 | tail -5 | sed 's/^/  /'
  rm -f "$TMP_MF"
  # Pin controller to the control-plane node. kind cluster.yaml maps host
  # :18080 → control-plane :80 (extraPortMappings), and the controller uses
  # hostPort 80 — so the pod MUST land on control-plane or the host port is a
  # black hole (TCP accepts → immediate RST). The upstream provider/kind
  # manifest historically pinned this via nodeSelector ingress-ready=true +
  # control-plane toleration, but `main` has drifted (observed 2026-05-30:
  # pod landed on sdlcma-dev-worker, curl http://localhost:18080/healthz
  # got "Connection reset by peer"). Re-applying the constraint here is
  # idempotent and robust to upstream drift — we own the invariant, not the
  # manifest.
  say "  pinning controller to control-plane (nodeSelector + toleration)"
  kubectl -n ingress-nginx patch deploy ingress-nginx-controller \
    --type=strategic \
    -p '{"spec":{"template":{"spec":{"nodeSelector":{"ingress-ready":"true"},"tolerations":[{"key":"node-role.kubernetes.io/control-plane","operator":"Equal","effect":"NoSchedule"}]}}}}' \
    2>&1 | sed 's/^/  /'
  # Admission Jobs run first (pull certgen via the proxy, create the
  # ingress-nginx-admission Secret), then the controller mounts that Secret
  # and starts. Wait on the controller's Ready condition.
  say "  waiting for ingress-nginx controller Ready (≤180s)"
  if ! kubectl -n ingress-nginx wait --for=condition=ready pod \
       -l app.kubernetes.io/component=controller --timeout=180s 2>&1 \
       | sed 's/^/  /'; then
    abort "ingress-nginx controller did not become Ready (debug: kubectl -n ingress-nginx get pods,events)"
  fi
  say "  ✓ ingress-nginx ready"
else
  say "4b: ingress-nginx skipped (INSTALL_INGRESS_NGINX=0)"
fi

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

# ── 5a: tailscale IP lookup for `minus` (when targeting that overlay) ──────
# The gitlab@minus overlay routes worker / orchestrator / gateway / llm-gw
# pods to a self-hosted GitLab on the operator's tailnet (hostname `minus`).
# Kind cluster pods don't see tailscale's MagicDNS, so we resolve the IP on
# the HOST side (where tailscale runs) and inject it via Helm's
# extraHostAliases — kindnet SNATs pod egress through the host's tailscale
# interface, completing the cross-intranet path.
#
# Detection: only trigger when the values file basename contains "minus".
# This keeps the gitlab_saas / other-overlay paths untouched.
HELM_HOSTALIAS_ARGS=()
case "$(basename "$VALUES_FILE")" in
  *minus*)
    say "5a: tailscale IP lookup for 'minus'"
    command -v tailscale >/dev/null || \
      abort "tailscale CLI missing — required for the gitlab@minus overlay"
    MINUS_IP="$(tailscale ip -4 minus 2>/dev/null | head -1 | tr -d '[:space:]')"
    [ -n "$MINUS_IP" ] || \
      abort "tailscale doesn't know about 'minus' (try: tailscale status). \
the gitlab@minus overlay requires minus to be a tailnet peer"
    say "  ✓ minus → $MINUS_IP"
    HELM_HOSTALIAS_ARGS=(
      --set "extraHostAliases[0].ip=$MINUS_IP"
      --set "extraHostAliases[0].hostnames[0]=minus"
    )
    ;;
  *)
    say "5a: tailscale lookup skipped (overlay isn't *minus*)"
    ;;
esac

# ── 6: helm dep update + helm upgrade --install ─────────────────────────────
# When monitoring.enabled=true (gitlab@minus default), the chart pulls in
# kube-prometheus-stack as a Helm dependency. `helm dep update` fetches the
# sub-chart tarball into charts/ before `helm install` reads it. Idempotent
# when the dep is already there — but the FIRST run on a fresh checkout has
# to do this, otherwise install fails with "found in Chart.yaml, but
# missing in charts/ directory".
say "6: helm dep update + helm upgrade --install $RELEASE"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null 2>&1 || true
helm dep update "$CHART_DIR" 2>&1 | tail -3 | sed 's/^/  /'
HELM_VALUES_ARGS=(-f "$VALUES_FILE")
if [ -f "$LOCAL_VALUES_FILE" ]; then
  say "  layering $LOCAL_VALUES_FILE on top of $VALUES_FILE"
  HELM_VALUES_ARGS+=(-f "$LOCAL_VALUES_FILE")
fi
# Timeout bumped from 3m → 5m: kube-prometheus-stack ships an
# Operator-managed Prometheus + Grafana sidecar that needs longer than
# the lean stack to settle. Still bounded.
helm upgrade --install "$RELEASE" "$CHART_DIR" \
  -n "$NAMESPACE" \
  "${HELM_VALUES_ARGS[@]}" \
  "${HELM_HOSTALIAS_ARGS[@]}" \
  --set namespace.create=false \
  --wait --timeout 5m 2>&1 | tail -5 | sed 's/^/  /'

# Whether cloudflared is on after the merged values — drives step 7's
# rollout list and step 8's URL extraction. Helm-rendering is the truth
# (deep-merges values.yaml + values-gitlab-saas.yaml + values.local.yaml).
CLOUDFLARED_ENABLED=1
if ! kubectl -n "$NAMESPACE" get deploy cloudflared >/dev/null 2>&1; then
  CLOUDFLARED_ENABLED=0
  say "  cloudflared disabled (no deploy/cloudflared) — webhook ingress via ingress-nginx only"
fi

# ── 7: rollouts ────────────────────────────────────────────────────────────
# Order matters: wait redis Ready FIRST so orchestrator's startup redis
# connection (and consumer-group XGROUP CREATE) actually lands against a live
# redis. Without this, when redis is the slowest pod to come up (observed on
# bare Ubuntu hosts where the third-party redis:7-alpine pull blocks helm
# --wait until images are kind-loaded), orchestrator can start with a broken
# redis client that does NOT auto-reconnect — the gateway:stream piles up
# but is never consumed. Restart-at-end (below) was the smoking-gun fix.
say "7: wait rollouts"
ROLLOUTS=(redis gateway orchestrator)
[ "$CLOUDFLARED_ENABLED" = "1" ] && ROLLOUTS+=(cloudflared)
# Conditional Deployments — detect by `kubectl get`. Cheaper + correct
# than re-parsing $VALUES_FILE; the cluster state is the truth.
for d in llm-gateway runrecord-exporter; do
  kubectl -n "$NAMESPACE" get deploy "$d" >/dev/null 2>&1 && ROLLOUTS+=("$d")
done
# kube-prometheus-stack-released Deployments (when monitoring.enabled=true).
# We wait on the Operator + Grafana + KSM — these exist immediately on
# helm install. Prometheus/Alertmanager are CRDs; the Operator stands them
# up after CRDs are applied, and they don't have a rollout status Helm
# understands, so we skip them here. `helm --wait` above guards the
# whole-stack invariant.
for d in "${RELEASE}-kube-prometheus-stack-operator" \
         "${RELEASE}-grafana" \
         "${RELEASE}-kube-state-metrics"; do
  kubectl -n "$NAMESPACE" get deploy "$d" >/dev/null 2>&1 && ROLLOUTS+=("$d")
done
for d in "${ROLLOUTS[@]}"; do
  kubectl -n "$NAMESPACE" rollout status "deploy/$d" --timeout=180s 2>&1 \
    | sed 's/^/  /'
done

# Defensive: roll the orchestrator once redis is confirmed Ready, in case its
# initial redis connection raced past an ImagePullBackOff redis. Idempotent
# no-op when ordering was already clean.
say "  refreshing orchestrator (redis-connection insurance)"
kubectl -n "$NAMESPACE" rollout restart deploy/orchestrator >/dev/null
kubectl -n "$NAMESPACE" rollout status deploy/orchestrator --timeout=60s 2>&1 \
  | sed 's/^/  /'

# ── 8: cloudflared URL (only if cloudflared is enabled) ────────────────────
URL=""
if [ "$CLOUDFLARED_ENABLED" = "1" ]; then
  say "8: extract trycloudflare URL"
  for i in $(seq 1 30); do  # ~2.5 min
    URL=$(kubectl -n "$NAMESPACE" logs deploy/cloudflared --tail=200 2>/dev/null \
      | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | sort -u | tail -1)
    [ -n "$URL" ] && break
    sleep 5
  done
  [ -n "$URL" ] || abort "cloudflared URL not visible after 2.5 min (kubectl logs deploy/cloudflared)" 4
  say "  ✓ tunnel URL: $URL"
else
  say "8: cloudflared URL extraction skipped (deployment not present)"
fi

echo
echo "=== READY ==="
echo "  cluster:     kind-$CLUSTER_NAME"
echo "  namespace:   $NAMESPACE"
[ -n "$URL" ] && echo "  webhook URL (cloudflared):        ${URL}/webhook"
if [ "$INSTALL_INGRESS_NGINX" = "1" ]; then
  echo "  webhook URL (ingress, host):      http://localhost:18080/webhook"
  TS_IP=""
  if command -v tailscale >/dev/null 2>&1; then
    TS_IP=$(tailscale ip -4 2>/dev/null | head -1)
  fi
  if [ -n "$TS_IP" ]; then
    echo "  webhook URL (ingress, tailnet):   http://${TS_IP}:18080/webhook"
  fi
fi
# Grafana / Prometheus URLs (only when monitoring.enabled rendered them).
# Prefer the Ingress path when a grafana Ingress exists (the values.local.yaml
# sub-path overlay shares ingress-nginx :18080 — e.g. /grafana); fall back to
# port-forward for the tracked default, which ships NO grafana Ingress. Cluster
# state is the truth — same detect-don't-assume pattern as cloudflared above.
if kubectl -n "$NAMESPACE" get svc "${RELEASE}-grafana" >/dev/null 2>&1; then
  G_PATH=$(kubectl -n "$NAMESPACE" get ingress "${RELEASE}-grafana" \
             -o jsonpath='{.spec.rules[0].http.paths[0].path}' 2>/dev/null || true)
  if [ -n "$G_PATH" ]; then
    echo "  Grafana (ingress, host):          http://localhost:18080${G_PATH}  (admin / admin — change in values.local.yaml)"
    [ -n "${TS_IP:-}" ] && echo "  Grafana (ingress, tailnet):       http://${TS_IP}:18080${G_PATH}"
  else
    echo "  Grafana (port-forward):           kubectl -n $NAMESPACE port-forward svc/${RELEASE}-grafana 3000:80"
    echo "                                    → http://localhost:3000  (admin / admin — change in values.local.yaml)"
  fi
fi
if kubectl -n "$NAMESPACE" get svc "${RELEASE}-kube-prometheus-stack-prometheus" >/dev/null 2>&1; then
  echo "  Prometheus (port-forward):        kubectl -n $NAMESPACE port-forward svc/${RELEASE}-kube-prometheus-stack-prometheus 9090:9090"
fi
echo
echo "Next:"
echo "  1. Configure the GitLab project webhook to ONE of the URLs above"
echo "     (Pipeline events). Pick the cloudflared URL when GitLab has no"
echo "     direct route to this host; pick an ingress URL (tailnet/LAN/"
echo "     hostname) when it does."
echo "  2. Trigger a failing CI on the test project, or run:"
echo "       PROJECT_PATH=user/repo bash infra/k8s/gitlab-smoke.sh"
echo "  3. Teardown: bash infra/k8s/teardown.sh"
