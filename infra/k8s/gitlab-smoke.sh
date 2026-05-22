#!/usr/bin/env bash
# ============================================================================
# infra/k8s/gitlab-smoke.sh — end-to-end smoke against gitlab.com.
#
# Assumes setup.sh has run and the gitlab.com webhook is pointed at the
# cluster's cloudflared URL. Trigger a pipeline on the test project, watch
# bf-worker Jobs in kind, verify a new auto/bf MR is opened.
#
# Env (defaults shown):
#   PROJECT_PATH=lishu20161/order_be     # repo slug on gitlab.com
#   ENV_FILE=settings/worker_gitlab_saas.env
#   NAMESPACE=sdlcma
#   TIMEOUT=600                          # seconds to wait for the MR
#
# Exit codes: 0 PASS / 2 FAIL / 3 TIMEOUT / 4 PRE-FLIGHT
# ============================================================================
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

PROJECT_PATH="${PROJECT_PATH:-lishu20161/order_be}"
ENV_FILE="${ENV_FILE:-settings/worker_gitlab_saas.env}"
NAMESPACE="${NAMESPACE:-sdlcma}"
TIMEOUT="${TIMEOUT:-600}"

GITLAB_API="https://gitlab.com/api/v4"
PROJ_ENC="${PROJECT_PATH//\//%2F}"

START_TS=$(date +%s)
say()   { printf '[t+%4ds] %s\n' $(($(date +%s) - START_TS)) "$*"; }
abort() { echo; echo "ABORT: $1" >&2; exit "${2:-4}"; }

[ -f "$ENV_FILE" ] || abort "missing $ENV_FILE"
TOK=$(grep -oE '^GITLAB_PRIVATE_TOKEN=.+' "$ENV_FILE" | cut -d= -f2- | tr -d '"')
[ -n "$TOK" ] || abort "GITLAB_PRIVATE_TOKEN missing from $ENV_FILE"

# Sanity-check the cluster is reachable.
kubectl -n "$NAMESPACE" get deploy/orchestrator >/dev/null 2>&1 \
  || abort "orchestrator deployment not found in ns $NAMESPACE — run setup.sh first"

# ── baseline MR iid (anything new must beat this) ──────────────────────────
# Defaults to 0 if the API call hiccups (gitlab.com 5xx happens) so the poll
# loop's `m['iid'] > $BASELINE` substitution never expands to invalid syntax.
BASELINE=$(curl -sf -m5 \
  "$GITLAB_API/projects/$PROJ_ENC/merge_requests?per_page=1&order_by=created_at&sort=desc" \
  -H "PRIVATE-TOKEN: $TOK" 2>/dev/null \
  | python3 -c "
import json, sys
try:
    mrs = json.load(sys.stdin)
    print(mrs[0]['iid'] if mrs else 0)
except Exception:
    print(0)
" 2>/dev/null)
BASELINE="${BASELINE:-0}"
say "baseline MR iid = $BASELINE"

# ── trigger pipeline ───────────────────────────────────────────────────────
PIPE=$(curl -sf -m5 -X POST "$GITLAB_API/projects/$PROJ_ENC/pipeline?ref=main" \
  -H "PRIVATE-TOKEN: $TOK" \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('id',''))")
[ -n "$PIPE" ] || abort "pipeline trigger failed (check token scopes: api+write_repository)" 2
say "triggered pipeline $PIPE on $PROJECT_PATH"
echo "  watch: https://gitlab.com/${PROJECT_PATH}/-/pipelines/${PIPE}"

# ── poll: MR created, while reporting Job count from kind ──────────────────
POLL_START=$(date +%s)
RESULT=TIMEOUT
while [ $(($(date +%s) - POLL_START)) -lt "$TIMEOUT" ]; do
  NEW_MR=$(curl -sf -m5 \
    "$GITLAB_API/projects/$PROJ_ENC/merge_requests?state=opened&order_by=created_at&sort=desc&per_page=3" \
    -H "PRIVATE-TOKEN: $TOK" \
    | python3 -c "
import json, sys
mrs = json.load(sys.stdin)
new = [m for m in mrs if m['iid'] > $BASELINE and m['source_branch'].startswith('auto/bf/')]
print('!{} {}  {}'.format(new[0]['iid'], new[0]['source_branch'], new[0]['web_url'])) if new else print('')
")
  if [ -n "$NEW_MR" ]; then
    RESULT=PASS
    say "✓ MR opened: $NEW_MR"
    break
  fi
  JOBS=$(kubectl -n "$NAMESPACE" get jobs -l app=bf-worker --no-headers 2>/dev/null | wc -l)
  ACT=$(kubectl -n "$NAMESPACE" get jobs -l app=bf-worker --no-headers 2>/dev/null \
        | awk '$2 ~ /^0\// {n++} END {print n+0}')
  say "  bf-worker jobs=${JOBS} active=${ACT}"
  sleep 15
done

echo
case "$RESULT" in
  PASS)    say "=== PASS ==="; exit 0 ;;
  TIMEOUT) say "=== TIMEOUT (no new auto/bf MR within ${TIMEOUT}s) ==="
           echo "  diagnose: kubectl -n $NAMESPACE logs deploy/orchestrator --tail=200"
           echo "            kubectl -n $NAMESPACE get jobs -l app=bf-worker"
           exit 3 ;;
esac
