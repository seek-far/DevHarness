#!/usr/bin/env bash
# ============================================================================
# infra/k3s/crossnode-check.sh — prove a real worker can run on the
# cross-continent node.
#
# WHY THIS EXISTS
# ---------------
# Without it, a cross-continent cluster ships validated only by busybox
# pings: `kubectl get nodes` Ready plus an MTU probe. Two paths that only a
# real workload exercises stay untested —
#
#   (a) pod on minus  →  redis POD on ls4900   (pod-to-pod over flannel/tailscale)
#   (b) pod on minus  →  ls4900's GitLab on :80 (pod → the OTHER node's host IP,
#                                                via extraHostAliases; NOT the
#                                                same path as (a))
#
# HOW (and why not the obvious way)
# ---------------------------------
# There is no manual "spawn a worker" entry point: the only trigger is a
# FAILED PIPELINE webhook (gateway → gateway:stream → orchestrator → parser →
# BugReportedEvent → spawner). So instead of hand-writing a Job — which would
# copy K8sJobSpawner._build_job's spec by hand, and a hand-copied spec keeps
# passing after the real one changes — this makes minus the only schedulable
# node and lets the REAL chain place the worker there:
#
#     remove the cross-continent taint  +  cordon ls4900
#     → trigger a failing pipeline
#     → assert the bf-worker pod's NODE == minus, and an auto/bf MR appears
#     → restore both nodes (trap), then VERIFY the restore took
#
# That also covers what a hand-made Job structurally cannot: the orchestrator's
# own scheduling decision, and cross-ocean heartbeat supervision (worker on
# minus → redis on ls4900 → HealthMonitor on ls4900). If the heartbeat drops
# out of its TTL and the monitor restarts the worker, that is a RESULT worth
# recording for W4 — not noise to hide.
#
# COST: while cordoned, ls4900 schedules no NEW pods (running ones are
# untouched, and the host's own ver99 subprocess stack never goes through k8s
# at all). Keep the window short; the trap restores on any exit path.
#
# Env (defaults shown):
#   K3S_KUBECONFIG=~/.kube/k3s.yaml   KCTX=default   NAMESPACE=sdlcma
#   SERVER_NODE=ls4900                REMOTE_NODE=minus
#   GITLAB_API=http://ls4900/api/v4   PROJECT_PATH=root/k3s-smoke
#   ENV_FILE=settings/worker_local_multi_process.env
#   TIMEOUT=900                       # total wait for the MR
#
# Exit: 0 PASS / 2 FAIL / 3 TIMEOUT (reported as PARTIAL) / 4 pre-flight
# ============================================================================
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

K3S_KUBECONFIG="${K3S_KUBECONFIG:-$HOME/.kube/k3s.yaml}"
KCTX="${KCTX:-default}"
NAMESPACE="${NAMESPACE:-sdlcma}"
SERVER_NODE="${SERVER_NODE:-ls4900}"
REMOTE_NODE="${REMOTE_NODE:-minus}"
GITLAB_API="${GITLAB_API:-http://ls4900/api/v4}"
PROJECT_PATH="${PROJECT_PATH:-root/k3s-smoke}"
ENV_FILE="${ENV_FILE:-settings/worker_local_multi_process.env}"
CROSS_TAINT_KEY="${CROSS_TAINT_KEY:-sdlcma.io/cross-continent}"
CROSS_TAINT="${CROSS_TAINT:-${CROSS_TAINT_KEY}=true:NoSchedule}"
TIMEOUT="${TIMEOUT:-900}"
PROJ_ENC="${PROJECT_PATH//\//%2F}"

START_TS=$(date +%s)
say()   { printf '[t+%4ds] %s\n' $(($(date +%s) - START_TS)) "$*"; }
abort() { echo; echo "ABORT: $1" >&2; exit "${2:-4}"; }

[ -r "$K3S_KUBECONFIG" ] || abort "no $K3S_KUBECONFIG — run infra/k3s/setup.sh first"
export KUBECONFIG="$K3S_KUBECONFIG"
kc() { kubectl --context "$KCTX" "$@"; }

[ -f "$ENV_FILE" ] || abort "missing $ENV_FILE"
TOK=$(grep -oE '^GITLAB_PRIVATE_TOKEN=.+' "$ENV_FILE" | cut -d= -f2- | tr -d '"')
[ -n "$TOK" ] || abort "GITLAB_PRIVATE_TOKEN missing from $ENV_FILE"

# ── pre-flight ─────────────────────────────────────────────────────────────
say "pre-flight"
kc -n "$NAMESPACE" get deploy/orchestrator >/dev/null 2>&1 \
  || abort "orchestrator not deployed in ns $NAMESPACE — run setup.sh first"
REM_READY=$(kc get node "$REMOTE_NODE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
[ "$REM_READY" = "True" ] || abort "$REMOTE_NODE is not Ready — nothing to check" 4

# The worker image must already be in the REMOTE node's containerd. There is
# no registry, and imagePullPolicy is IfNotPresent, so a missing image gives
# ImagePullBackOff — which fails SILENTLY end-to-end (the Job is neither
# succeeded nor failed, so the spawner's returncode stays None; the worker
# never heartbeats; 120s later the monitor marks it failed without ever
# naming the image). Say so up front instead.
cat <<EOF
  NOTE: this requires dh-bf-worker:latest in ${REMOTE_NODE}'s containerd:
        bash infra/k3s/load-image.sh --node $REMOTE_NODE
        (verify on $REMOTE_NODE: sudo k3s ctr -n k8s.io images ls | grep dh-bf-worker)
EOF

# ── snapshot node state BEFORE mutating, so restore is exact ───────────────
PRIOR_TAINTS=$(kc get node "$REMOTE_NODE" -o jsonpath='{.spec.taints}' 2>/dev/null)
PRIOR_CORDON=$(kc get node "$SERVER_NODE" -o jsonpath='{.spec.unschedulable}' 2>/dev/null)
say "snapshot: $REMOTE_NODE taints=${PRIOR_TAINTS:-none} / $SERVER_NODE unschedulable=${PRIOR_CORDON:-false}"

RESTORED=0
restore() {
  [ "$RESTORED" = 1 ] && return
  RESTORED=1
  echo
  say "restoring node scheduling state"
  # Re-taint first: if anything goes wrong after this point, the safe state
  # is "workers cannot land on the cross-continent node".
  kc taint node "$REMOTE_NODE" "$CROSS_TAINT" --overwrite >/dev/null 2>&1
  if [ "${PRIOR_CORDON:-}" != "true" ]; then
    kc uncordon "$SERVER_NODE" >/dev/null 2>&1
  fi
  # Verify the restore actually took — issuing the command is not the same
  # as the state having changed, and leaving this box cordoned would break
  # every later deploy in a way that looks unrelated.
  local t c
  t=$(kc get node "$REMOTE_NODE" -o jsonpath='{.spec.taints[*].key}' 2>/dev/null)
  c=$(kc get node "$SERVER_NODE" -o jsonpath='{.spec.unschedulable}' 2>/dev/null)
  if [[ "$t" == *"$CROSS_TAINT_KEY"* ]] && [ "${c:-false}" != "true" ]; then
    say "  ✓ restored ($REMOTE_NODE tainted, $SERVER_NODE schedulable)"
  else
    echo "  !! RESTORE INCOMPLETE — fix by hand:" >&2
    echo "     kubectl taint node $REMOTE_NODE $CROSS_TAINT --overwrite" >&2
    echo "     kubectl uncordon $SERVER_NODE" >&2
  fi
}
trap restore EXIT INT TERM

# ── open the window ────────────────────────────────────────────────────────
say "making $REMOTE_NODE the only schedulable node"
kc taint node "$REMOTE_NODE" "${CROSS_TAINT_KEY}-" >/dev/null 2>&1
kc cordon "$SERVER_NODE" >/dev/null || abort "cordon $SERVER_NODE failed" 2
say "  ✓ $REMOTE_NODE untainted, $SERVER_NODE cordoned"

# ── trigger: a failed pipeline is the ONLY way to spawn a worker ───────────
BASELINE=$(curl -sf -m10 \
  "$GITLAB_API/projects/$PROJ_ENC/merge_requests?per_page=1&order_by=created_at&sort=desc" \
  -H "PRIVATE-TOKEN: $TOK" 2>/dev/null \
  | python3 -c "
import json,sys
try:
    mrs = json.load(sys.stdin); print(mrs[0]['iid'] if mrs else 0)
except Exception:
    print(0)
" 2>/dev/null)
BASELINE="${BASELINE:-0}"
PIPE=$(curl -sf -m10 -X POST "$GITLAB_API/projects/$PROJ_ENC/pipeline?ref=main" \
  -H "PRIVATE-TOKEN: $TOK" \
  | python3 -c "import json,sys;print(json.load(sys.stdin).get('id',''))")
[ -n "$PIPE" ] || abort "pipeline trigger failed on $PROJECT_PATH" 2
say "triggered pipeline $PIPE (baseline MR iid=$BASELINE)"

# ── poll ───────────────────────────────────────────────────────────────────
# Assertion order matters. Every failure below looks the same from the
# outside — "no MR appeared" — and the tempting story is always "the ocean
# link is bad". Check our own side first, in causal order, and only blame
# the network when the three earlier links are proven good.
WORKER_NODE=""
RESULT=TIMEOUT
while [ $(($(date +%s) - START_TS)) -lt "$TIMEOUT" ]; do
  if [ -z "$WORKER_NODE" ]; then
    WORKER_NODE=$(kc -n "$NAMESPACE" get pods -l app=bf-worker \
      -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' 2>/dev/null | grep . | tail -1)
    [ -n "$WORKER_NODE" ] && say "  worker pod scheduled on: $WORKER_NODE"
  fi
  NEW_MR=$(curl -sf -m10 \
    "$GITLAB_API/projects/$PROJ_ENC/merge_requests?state=opened&order_by=created_at&sort=desc&per_page=3" \
    -H "PRIVATE-TOKEN: $TOK" \
    | python3 -c "
import json,sys
mrs = json.load(sys.stdin)
new = [m for m in mrs if m['iid'] > $BASELINE and m['source_branch'].startswith('auto/bf/')]
print('!{} {}  {}'.format(new[0]['iid'], new[0]['source_branch'], new[0]['web_url'])) if new else print('')
" 2>/dev/null)
  if [ -n "$NEW_MR" ]; then
    RESULT=PASS
    say "✓ MR opened: $NEW_MR"
    break
  fi
  PODS=$(kc -n "$NAMESPACE" get pods -l app=bf-worker --no-headers 2>/dev/null \
         | awk '{printf "%s(%s on %s) ", $1, $3, "?"}' | head -c 200)
  say "  waiting… bf-worker pods: ${PODS:-none}"
  sleep 15
done

# ── verdict ────────────────────────────────────────────────────────────────
echo
if [ "$RESULT" = PASS ] && [ "$WORKER_NODE" = "$REMOTE_NODE" ]; then
  say "=== PASS === a real worker ran on $REMOTE_NODE and opened an MR"
  say "    cross-ocean paths proven: pod→redis(pod), pod→GitLab(host), CI result routing"
  RC=0
elif [ "$RESULT" = PASS ]; then
  say "=== FAIL === an MR appeared but the worker ran on '${WORKER_NODE:-unknown}',"
  say "    not $REMOTE_NODE. Nothing cross-ocean was proven."
  RC=2
else
  say "=== PARTIAL/TIMEOUT === no auto/bf MR within ${TIMEOUT}s."
  say "    Diagnose IN THIS ORDER (the last one is the only network story):"
  echo "      1. webhook reached the orchestrator and parsed as a bug?"
  echo "         kubectl -n $NAMESPACE logs deploy/orchestrator | grep -E 'BugReportedEvent|spawn'"
  echo "      2. a Job was created and its pod landed on $REMOTE_NODE?"
  echo "         kubectl -n $NAMESPACE get pods -l app=bf-worker -o wide"
  echo "      3. the worker container actually started (image present on $REMOTE_NODE)?"
  echo "         kubectl -n $NAMESPACE describe pod -l app=bf-worker | tail -30"
  echo "      4. only if 1-3 are all good, suspect the cross-ocean link / CI."
  echo "    Also worth recording for W4: did the monitor restart the worker?"
  echo "         kubectl -n $NAMESPACE logs deploy/orchestrator | grep heartbeat"
  RC=3
fi
exit $RC
