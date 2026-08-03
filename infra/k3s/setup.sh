#!/usr/bin/env bash
# ============================================================================
# infra/k3s/setup.sh — bring the SDLCMA stack up on the cross-continent k3s
# cluster (ls4900 = server/CN, minus = agent/DE).
#
# This is the k3s sibling of infra/k8s/setup.sh (kind). The differences are
# all consequences of "real multi-node, on hosts that already run other
# things":
#
#   kind version                     k3s version
#   ─────────────────────────────    ────────────────────────────────────────
#   kind create cluster              nothing — the cluster is a systemd unit
#                                    (installed once by hand; docs/k3s.md §2)
#   kind load docker-image           load-image.sh (containerd, -n k8s.io)
#   installs ingress-nginx           no ingress — gateway Service is NodePort
#   kubectl config use-context       a DEDICATED kubeconfig, never a global
#                                    context switch (step 0)
#   —                                taints the cross-continent node (step 3)
#   —                                isolation pre-flight vs the host's own
#                                    ver99 stack (step 1b)
#
# Idempotent: re-runs without harm.
#
# Env overrides (defaults shown):
#   K3S_KUBECONFIG=~/.kube/k3s.yaml     dedicated; never touches ~/.kube/config
#   KCTX=default                        k3s's context name
#   NAMESPACE=sdlcma  RELEASE=sdlcma
#   CHART_DIR=infra/helm/sdlcma
#   VALUES_FILE=infra/helm/sdlcma/values-k3s-ls4900.yaml
#   LOCAL_VALUES_FILE=infra/helm/sdlcma/values.local.yaml   (gitignored, optional)
#   ENV_FILE=settings/worker_local_multi_process.env
#   SERVER_NODE=ls4900  REMOTE_NODE=minus
#   CROSS_TAINT=sdlcma.io/cross-continent=true:NoSchedule
#   LOAD_REMOTE=0                       1 → also import images on REMOTE_NODE
#
# Exit codes: 0 ready / 4 pre-flight fail / 10 needs a human (sudo etc.)
# ============================================================================
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

K3S_KUBECONFIG="${K3S_KUBECONFIG:-$HOME/.kube/k3s.yaml}"
KCTX="${KCTX:-default}"
NAMESPACE="${NAMESPACE:-sdlcma}"
RELEASE="${RELEASE:-sdlcma}"
CHART_DIR="${CHART_DIR:-infra/helm/sdlcma}"
VALUES_FILE="${VALUES_FILE:-infra/helm/sdlcma/values-k3s-ls4900.yaml}"
# Per-host overlay, layered on top of $VALUES_FILE when present. Deliberately
# NOT values.local.yaml: that file belongs to the kind harness, and on this
# host it carries kind-era settings — a Grafana Ingress with
# ingressClassName: nginx and root_url http://localhost:18080/grafana. There
# is no ingress controller here (traefik is disabled) and :18080 died with the
# kind cluster, so layering it would install a resource nothing reconciles,
# pointing at a URL that does not exist. Inert today only because this overlay
# ships monitoring.enabled=false; it would go live the moment monitoring is
# switched on.
#
# Same class of mistake as sharing ~/.kube/config between the two harnesses
# (see step 0): per-host state must not be shared across deployment shapes.
# values.local-*.yaml is gitignored too.
LOCAL_VALUES_FILE="${LOCAL_VALUES_FILE:-infra/helm/sdlcma/values.local-k3s.yaml}"
ENV_FILE="${ENV_FILE:-settings/worker_local_multi_process.env}"
SERVER_NODE="${SERVER_NODE:-ls4900}"
REMOTE_NODE="${REMOTE_NODE:-minus}"
CROSS_TAINT="${CROSS_TAINT-sdlcma.io/cross-continent=true:NoSchedule}"
# The key alone, needed to REMOVE the taint (`key-`) when CROSS_TAINT is empty.
# Kept separate so "" can mean "converge to absent" rather than "unset".
CROSS_TAINT_KEY="${CROSS_TAINT_KEY:-sdlcma.io/cross-continent}"
LOAD_REMOTE="${LOAD_REMOTE:-0}"
IMAGES=(dh-gateway dh-orchestrator dh-bf-worker dh-llm-gateway)

# ── --ver99: run the SWE-bench workload on this cluster (plan item W4) ──────
# Three coupled changes; doing any subset leaves a config that looks enabled
# and is not:
#   1. layer values-k3s-ver99.yaml  (worker image + docker.sock + resources)
#   2. drop the cross-continent taint so worker Jobs can use both nodes
#   3. pre-flight the swebench image, whose absence would otherwise surface as
#      a silent ImagePullBackOff
VER99=0
SKIP_IMAGES=0
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --ver99) VER99=1; shift ;;
    # Config-only re-run: skip build+import entirely. The import step cannot
    # vouch for a moving tag without sudo (see load-image.sh), so on a
    # password-gated host EVERY re-run would otherwise cost a ~2 GB
    # save/import plus a human — even when only a ConfigMap value changed.
    # This flag is the operator ASSERTING the images are unchanged; it is not
    # a check, which is why it has to be typed rather than inferred.
    --skip-images) SKIP_IMAGES=1; shift ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
VER99_VALUES_FILE="${VER99_VALUES_FILE:-infra/helm/sdlcma/values-k3s-ver99.yaml}"
if [ "$VER99" = 1 ]; then
  CROSS_TAINT=""
fi

START_TS=$(date +%s)
say()   { printf '[t+%4ds] %s\n' $(($(date +%s) - START_TS)) "$*"; }
abort() { echo; echo "ABORT: $1" >&2; exit "${2:-4}"; }

# ── 0: dedicated kubeconfig ────────────────────────────────────────────────
# NEVER `kubectl config use-context`. On this host ~/.kube/config was
# overwritten by k3s's own k3s.yaml when the cluster was installed, which
# silently deleted the kind context — infra/k8s/teardown.sh then became a
# no-op that exits 0 having torn down nothing. Two deployment shapes sharing
# one kubeconfig is how that happens; a dedicated file is how it doesn't.
say "0: kubeconfig"
if [ ! -r "$K3S_KUBECONFIG" ]; then
  cat <<EOF

!! $K3S_KUBECONFIG missing. Copy k3s's admin kubeconfig (needs sudo once):

     mkdir -p "\$(dirname "$K3S_KUBECONFIG")"
     sudo install -o "\$(id -u)" -g "\$(id -g)" -m 600 \\
       /etc/rancher/k3s/k3s.yaml "$K3S_KUBECONFIG"

   Deliberately NOT ~/.kube/config, and deliberately not chmod'ing the
   original (it is cluster-admin credentials).
EOF
  exit 10
fi
export KUBECONFIG="$K3S_KUBECONFIG"
kc() { kubectl --context "$KCTX" "$@"; }
say "  ✓ $K3S_KUBECONFIG (context=$KCTX)"

# ── 0b: ver99 pre-flight ───────────────────────────────────────────────────
# Resolve and validate the worker image ONCE, up front. It has to happen before
# the image-import step (which checks whether the remote node has it) and
# before helm — deriving it lazily at the helm step left the import step
# referencing an unset variable, which `set -u` turns into a hard stop after
# the run has already changed the cluster's taint.
WORKER_IMAGE_TAG=""
if [ "$VER99" = 1 ]; then
  [ -f "$VER99_VALUES_FILE" ] || abort "--ver99 needs $VER99_VALUES_FILE"
  WORKER_IMAGE_TAG=$(grep -oE 'dh-bf-worker-swebench:[A-Za-z0-9._-]+' "$VER99_VALUES_FILE" | head -1)
  case "$WORKER_IMAGE_TAG" in
    *REPLACE_ME|"")
      abort "$VER99_VALUES_FILE still has the placeholder WORKER_IMAGE.
   Build the image and paste the tag it prints:
     bash infra/k3s/build-swebench-image.sh" ;;
    *:latest)
      # Not pedantry: imagePullPolicy is IfNotPresent and images are
      # side-loaded (no registry to re-pull from), so a moving tag lets two
      # nodes hold different content under one name with nothing to reveal it.
      abort "$VER99_VALUES_FILE references a :latest tag. Use the immutable
   tag from build-swebench-image.sh." ;;
  esac
  docker image inspect "$WORKER_IMAGE_TAG" >/dev/null 2>&1 \
    || abort "$WORKER_IMAGE_TAG is not in the local docker daemon.
   Build it:  bash infra/k3s/build-swebench-image.sh"
  say "0b: ver99 mode — worker image $WORKER_IMAGE_TAG"
fi

# ── 1: deps + creds ────────────────────────────────────────────────────────
say "1: deps + creds"
for c in docker helm kubectl tailscale; do
  command -v "$c" >/dev/null || abort "missing dep: $c"
done
docker info >/dev/null 2>&1 || abort "docker daemon unreachable"
[ -f "$ENV_FILE" ] || abort "missing $ENV_FILE"
TOK=$(grep -oE '^GITLAB_PRIVATE_TOKEN=.+' "$ENV_FILE" | cut -d= -f2- | tr -d '"')
KEY=$(grep -oE '^LLM_API_KEY=.+'          "$ENV_FILE" | cut -d= -f2- | tr -d '"')
[ -n "$TOK" ] || abort "GITLAB_PRIVATE_TOKEN missing from $ENV_FILE"
[ -n "$KEY" ] || abort "LLM_API_KEY missing from $ENV_FILE"
[ -f "$VALUES_FILE" ] || abort "missing $VALUES_FILE"
say "  ✓ deps + creds"

# ── 1b: isolation pre-flight ───────────────────────────────────────────────
# This host also runs the ver99 stack as plain host subprocesses. Two
# orchestrators sharing one redis + one consumer group means they share a
# PEL, steal each other's pending entries, and spawn duplicate workers for
# the same bug — silent data corruption, not a crash. The cluster stack must
# therefore use its OWN redis (the in-cluster Service), and a different
# GitLab project.
say "1b: isolation pre-flight"
# Check the RENDERED value, not the overlay text: the overlay usually doesn't
# mention REDIS_URL at all (it inherits values.yaml), so grepping the file
# would be a check that can only ever pass. `helm template` is the merged
# truth and costs nothing here.
RENDERED_REDIS=$(helm template "$RELEASE" "$CHART_DIR" -f "$VALUES_FILE" 2>/dev/null \
                 | grep -E '^\s+REDIS_URL:' | sed 's/^[[:space:]]*//' | sort -u)
[ -n "$RENDERED_REDIS" ] || abort "could not render REDIS_URL from $VALUES_FILE (helm template failed?)"
if printf '%s\n' "$RENDERED_REDIS" | grep -qv 'redis://redis:'; then
  echo "  rendered: $RENDERED_REDIS" >&2
  abort "REDIS_URL points outside the cluster. This host also runs the ver99
  stack as host subprocesses; two orchestrators on one redis share the
  'orchestrator-group' consumer group and its PEL, steal each other's pending
  entries, and spawn DUPLICATE workers for the same bug — silent corruption,
  not a crash. Use the in-cluster Service (redis://redis:6379/N)."
fi
say "  ✓ redis is in-cluster: $(printf '%s' "$RENDERED_REDIS" | head -1)"
if command -v ss >/dev/null && ss -lnt 2>/dev/null | grep -qE ':8000[[:space:]]'; then
  say "  ⚠ host :8000 is listening — the ver99 host stack is probably up."
  say "    That is FINE (different redis, different project) but you now have"
  say "    two orchestrators on this box. Keep their GitLab projects disjoint."
fi
say "  ✓ isolation checked"

# ── 2: cluster health ──────────────────────────────────────────────────────
say "2: cluster health"
kc get nodes >/dev/null 2>&1 || abort "cannot reach the k3s API via $K3S_KUBECONFIG"
SRV_STATUS=$(kc get node "$SERVER_NODE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
[ "$SRV_STATUS" = "True" ] || abort "$SERVER_NODE is not Ready"
REM_STATUS=$(kc get node "$REMOTE_NODE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
if [ "$REM_STATUS" = "True" ]; then
  say "  ✓ $SERVER_NODE Ready, $REMOTE_NODE Ready"
else
  # Not fatal: the cross-continent node is expected to come and go (it is a
  # WSL2 guest behind a tailnet). Every service is pinned to the server node,
  # so the stack installs and runs fine without it. Only A6b needs it.
  say "  ⚠ $REMOTE_NODE not Ready — continuing (all services are pinned to"
  say "    $SERVER_NODE). The cross-node check will not work until it returns."
fi

# ── 3: cross-continent taint — converge to the mode we were asked for ──────
# W3 (default): the taint is ON. Worker Jobs then carried no resources and no
# nodeSelector, so without it the scheduler treated a 15.6 GB WSL2 guest on
# another continent as an equally good home for them — by coin flip, on every
# webhook. The taint was a DECLARED default, greppable and reversible;
# relying on "that node happens to lack the image" instead produces
# ImagePullBackOff, which fails silently (Job neither succeeds nor fails →
# returncode stays None → warmup timeout → marked failed, cause unnamed).
#
# W4 (--ver99, or CROSS_TAINT=""): the taint comes OFF. That was always the
# plan — it was a time-boxed stopgap, and leaving it would turn a temporary
# measure into a permanent ban on the thing W4 exists to enable. What replaces
# it is per-Job `resources` + `nodeSelector`, and — for the stateful side —
# `nodeSelector` on every component that owns a PVC, pinned by a chart test.
#
# CONVERGES IN BOTH DIRECTIONS on purpose: going W3→W4 and back must be a
# re-run of this script, not a hand-edited cluster.
if [ -n "$CROSS_TAINT" ]; then
  say "3: taint $REMOTE_NODE ($CROSS_TAINT)"
  if kc get node "$REMOTE_NODE" >/dev/null 2>&1; then
    kc taint node "$REMOTE_NODE" "$CROSS_TAINT" --overwrite >/dev/null \
      && say "  ✓ tainted (remove with: kubectl taint node $REMOTE_NODE ${CROSS_TAINT%%=*}-)"
  else
    say "  ⚠ $REMOTE_NODE not in the cluster, skipping"
  fi
else
  say "3: cross-continent taint OFF (worker Jobs may schedule on $REMOTE_NODE)"
  if kc get node "$REMOTE_NODE" >/dev/null 2>&1; then
    # `key-` removes it if present and is a no-op if not, so this is safe to
    # re-run. The default taint key is derived from the default value of
    # CROSS_TAINT, since CROSS_TAINT itself is empty here.
    kc taint node "$REMOTE_NODE" "${CROSS_TAINT_KEY}-" >/dev/null 2>&1
    say "  ✓ $REMOTE_NODE carries no ${CROSS_TAINT_KEY} taint"
    say "    NOTE: a NoSchedule taint never evicts running pods — to put the"
    say "    stopgap back, re-run without --ver99 AND delete in-flight Jobs."
  else
    say "  ⚠ $REMOTE_NODE not in the cluster, skipping"
  fi
fi

# ── 4: build images ────────────────────────────────────────────────────────
if [ "$SKIP_IMAGES" = 1 ]; then
  say "4-5: --skip-images — not building, not importing (operator asserts unchanged)"
else
say "4: build images"
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
# Best-effort refresh. On a CN host docker.io is frequently unreachable, and
# the daemon usually has the image already (it is the same one the kind path
# used). A failed pull here is not fatal — load-image.sh `docker image
# inspect`s every image before importing and aborts with a clear message if
# one is genuinely absent — so don't let a network timeout look like a build
# failure.
if docker pull redis:7-alpine >/dev/null 2>&1; then
  say "  ✓ redis:7-alpine pulled"
elif docker image inspect redis:7-alpine >/dev/null 2>&1; then
  say "  ✓ redis:7-alpine already local (pull unreachable — fine)"
else
  abort "redis:7-alpine is neither pullable nor present locally"
fi

# ── 5: import into containerd ──────────────────────────────────────────────
say "5: import images into containerd"
LOAD_ARGS=(--node local)
[ "$LOAD_REMOTE" = "1" ] && LOAD_ARGS=(--node all)
bash infra/k3s/load-image.sh "${LOAD_ARGS[@]}" | sed 's/^/  /'
LOAD_RC=${PIPESTATUS[0]}
[ "$LOAD_RC" -eq 10 ] && exit 10
[ "$LOAD_RC" -eq 0 ] || abort "image import failed (rc=$LOAD_RC)"

# With --ver99 the taint is gone, so worker Jobs can land on the remote node —
# and if the image never got imported there, the pod sits in ImagePullBackOff
# and the failure is SILENT (the Job neither succeeds nor fails, so the
# orchestrator only sees a warmup timeout). Check rather than ship: a ~1.5 GB
# cross-ocean transfer on every idempotent re-run is not acceptable, and
# `node.status.images` answers the question with no ssh at all.
if [ "$VER99" = 1 ] && kc get node "$REMOTE_NODE" >/dev/null 2>&1; then
  # `names[*]`, and match the registry-qualified form: containerd normalises
  # `dh-bf-worker-swebench:<sha>` to `docker.io/library/dh-bf-worker-swebench:<sha>`
  # on import, so an exact compare against the bare name never hits.
  if kc get node "$REMOTE_NODE" -o jsonpath='{.status.images[*].names[*]}' 2>/dev/null \
       | tr ' ' '\n' | grep -qE "(^|/)${WORKER_IMAGE_TAG}\$"; then
    say "  ✓ $WORKER_IMAGE_TAG present on $REMOTE_NODE"
  else
    say "  ⚠ $WORKER_IMAGE_TAG is NOT on $REMOTE_NODE — workers scheduled there"
    say "    will ImagePullBackOff (silently). Ship it once:"
    say "      bash infra/k3s/load-image.sh --node $REMOTE_NODE $WORKER_IMAGE_TAG"
  fi
fi
fi   # end of --skip-images guard

# ── 6: namespace + secret ──────────────────────────────────────────────────
say "6: namespace + sdlcma-secrets"
kc create namespace "$NAMESPACE" --dry-run=client -o yaml | kc apply -f - >/dev/null
kc -n "$NAMESPACE" delete secret sdlcma-secrets --ignore-not-found >/dev/null
kc -n "$NAMESPACE" create secret generic sdlcma-secrets \
  --from-literal="GITLAB_PRIVATE_TOKEN=$TOK" \
  --from-literal="LLM_API_KEY=$KEY" >/dev/null
say "  ✓ secret created"

# ── 7: helm install ────────────────────────────────────────────────────────
# Pods can't resolve `ls4900` (no MagicDNS inside the cluster), so resolve it
# host-side and inject a hostAlias. Same seam infra/k8s/setup.sh uses for
# `minus`; here the target happens to be this machine itself.
say "7: helm upgrade --install $RELEASE"
TS_IP="$(tailscale ip -4 2>/dev/null | head -1 | tr -d '[:space:]')"
[ -n "$TS_IP" ] || abort "tailscale ip -4 returned nothing — the overlay needs it for hostAliases"
say "  $SERVER_NODE → $TS_IP"
HELM_VALUES_ARGS=(-f "$VALUES_FILE")
if [ "$VER99" = 1 ]; then
  say "  layering $VER99_VALUES_FILE (worker image $WORKER_IMAGE_TAG)"
  HELM_VALUES_ARGS+=(-f "$VER99_VALUES_FILE")
fi
[ -f "$LOCAL_VALUES_FILE" ] && { say "  layering $LOCAL_VALUES_FILE"; HELM_VALUES_ARGS+=(-f "$LOCAL_VALUES_FILE"); }
# Only needed when monitoring.enabled=true (kube-prometheus-stack is a
# conditional dependency). Harmless otherwise, and doing it unconditionally
# means flipping monitoring on later doesn't need a different command.
helm dep update "$CHART_DIR" >/dev/null 2>&1 || true
helm --kube-context "$KCTX" upgrade --install "$RELEASE" "$CHART_DIR" \
  -n "$NAMESPACE" \
  "${HELM_VALUES_ARGS[@]}" \
  --set "extraHostAliases[0].ip=$TS_IP" \
  --set "extraHostAliases[0].hostnames[0]=$SERVER_NODE" \
  --set namespace.create=false \
  --wait --timeout 5m 2>&1 | tail -5 | sed 's/^/  /'

# ── 8: rollouts ────────────────────────────────────────────────────────────
# redis first: the orchestrator opens its redis connection (and creates the
# consumer group) at startup and does NOT auto-reconnect, so a redis that is
# still pulling when the orchestrator starts leaves a live pod that never
# consumes gateway:stream. The restart at the end is the cheap insurance.
say "8: wait rollouts"
ROLLOUTS=(redis gateway orchestrator)
for d in llm-gateway runrecord-exporter; do
  kc -n "$NAMESPACE" get deploy "$d" >/dev/null 2>&1 && ROLLOUTS+=("$d")
done
for d in "${ROLLOUTS[@]}"; do
  kc -n "$NAMESPACE" rollout status "deploy/$d" --timeout=180s 2>&1 | sed 's/^/  /'
done
# Also covers the :latest re-import case: kubelet does not re-resolve a tag
# it already has, so a freshly imported image needs a restart to take effect.
say "  refreshing orchestrator + gateway"
kc -n "$NAMESPACE" rollout restart deploy/orchestrator deploy/gateway >/dev/null
# Wait for BOTH — restarting two and waiting on one leaves the other mid-swap
# when the summary below prints, which shows two pods per Deployment and reads
# like a scheduling bug that isn't there.
for d in orchestrator gateway; do
  kc -n "$NAMESPACE" rollout status "deploy/$d" --timeout=90s 2>&1 | sed 's/^/  /'
done

# ── 9: summary ─────────────────────────────────────────────────────────────
NODE_PORT=$(kc -n "$NAMESPACE" get svc gateway -o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null)
echo
echo "=== READY ==="
echo "  kubeconfig:  $K3S_KUBECONFIG (context=$KCTX)"
echo "  namespace:   $NAMESPACE"
echo "  webhook URL: http://${TS_IP}:${NODE_PORT:-<none>}/webhook"
echo
echo "  placement (all services pinned to $SERVER_NODE):"
kc -n "$NAMESPACE" get pods -o wide --no-headers 2>/dev/null \
  | awk '{printf "    %-34s %-10s %s\n", $1, $3, $7}'
echo
echo "Next:"
echo "  1. Point the GitLab project webhook (Pipeline events) at the URL above."
echo "  2. Smoke:      GITLAB_API=http://${SERVER_NODE}/api/v4 PROJECT_PATH=root/k3s-smoke \\"
echo "                   bash infra/k8s/gitlab-smoke.sh"
echo "  3. Cross-node: bash infra/k3s/crossnode-check.sh"
echo "  4. Teardown:   bash infra/k3s/teardown.sh"
