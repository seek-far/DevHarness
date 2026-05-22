#!/usr/bin/env bash
# ============================================================================
# infra/k8s/regression.sh — one-command end-to-end regression on kind.
#
# Phases:
#   1 env check        (deps + token)
#   2 setup            (delegates to setup.sh: kind + build + load + helm)
#   3 webhook rewrite  (point the gitlab.com project hook at our cloudflared)
#   4 smoke            (delegates to gitlab-smoke.sh)
#   5 restore          (revert webhook; teardown unless --no-teardown)
#
# Exit codes: 0 PASS / 2 FAIL / 3 TIMEOUT / 4 PRE-FLIGHT
#
# Usage:
#   bash infra/k8s/regression.sh \
#     [--timeout 900] [--no-teardown] [--keep-cluster] [--keep-webhook]
# ============================================================================
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

TIMEOUT=900
NO_TEARDOWN=0
KEEP_CLUSTER=0
KEEP_WEBHOOK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --timeout)       TIMEOUT="$2"; shift 2 ;;
    --no-teardown)   NO_TEARDOWN=1; shift ;;
    --keep-cluster)  KEEP_CLUSTER=1; shift ;;
    --keep-webhook)  KEEP_WEBHOOK=1; shift ;;
    -h|--help)       sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 4 ;;
  esac
done

NAMESPACE="${NAMESPACE:-sdlcma}"
PROJECT_PATH="${PROJECT_PATH:-lishu20161/order_be}"
HOOK_ID="${HOOK_ID:-78655514}"   # reused from AWS ECS regression on same repo
ENV_FILE="${ENV_FILE:-settings/worker_gitlab_saas.env}"
GITLAB_API="https://gitlab.com/api/v4"
PROJ_ENC="${PROJECT_PATH//\//%2F}"

START_TS=$(date +%s)
say()   { printf '[t+%4ds] %s\n' $(($(date +%s) - START_TS)) "$*"; }
abort() { echo; echo "ABORT: $1" >&2; exit "${2:-4}"; }

# ── 1: env check ───────────────────────────────────────────────────────────
say "Phase 1: env check"
for c in docker kind helm kubectl curl python3; do
  command -v "$c" >/dev/null || abort "missing dep: $c"
done
[ -f "$ENV_FILE" ] || abort "missing $ENV_FILE"
TOK=$(grep -oE '^GITLAB_PRIVATE_TOKEN=.+' "$ENV_FILE" | cut -d= -f2- | tr -d '"')
[ -n "$TOK" ] || abort "GITLAB_PRIVATE_TOKEN missing from $ENV_FILE"
say "  ✓ deps + token"

# Capture pre-state for restore (run BEFORE any mutation so we can roll back).
PRIOR_HOOK=$(curl -sf -m5 "$GITLAB_API/projects/$PROJ_ENC/hooks/$HOOK_ID" \
   -H "PRIVATE-TOKEN: $TOK" \
   | python3 -c "import json,sys;print(json.load(sys.stdin).get('url',''))" 2>/dev/null || echo "")

restore() {
  local rc=$?
  set +e
  if [ "$KEEP_WEBHOOK" = 0 ] && [ -n "$PRIOR_HOOK" ]; then
    say "Phase 5a: webhook restore → $PRIOR_HOOK"
    curl -sf -m5 -X PUT "$GITLAB_API/projects/$PROJ_ENC/hooks/$HOOK_ID" \
      -H "PRIVATE-TOKEN: $TOK" -H "Content-Type: application/json" \
      -d "{\"url\":\"$PRIOR_HOOK\",\"pipeline_events\":true,\"push_events\":false}" \
      >/dev/null && say "  ✓ restored"
  fi
  if [ "$NO_TEARDOWN" = 0 ]; then
    say "Phase 5b: teardown"
    local args=()
    [ "$KEEP_CLUSTER" = 1 ] && args+=(--keep-cluster)
    bash "$REPO_DIR/infra/k8s/teardown.sh" "${args[@]}" 2>&1 | sed 's/^/  /'
  else
    say "Phase 5b: --no-teardown → cluster + release retained"
  fi
  exit "$rc"
}
trap restore EXIT

# ── 2: setup ───────────────────────────────────────────────────────────────
say "Phase 2: setup (kind + build + load + helm)"
bash "$REPO_DIR/infra/k8s/setup.sh" 2>&1 | sed 's/^/  /' \
  || abort "setup.sh failed" 2

# Re-extract the cloudflared URL (setup printed it; reread for safety).
URL=$(kubectl -n "$NAMESPACE" logs deploy/cloudflared --tail=200 2>/dev/null \
  | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | sort -u | tail -1)
[ -n "$URL" ] || abort "no cloudflared URL after setup" 2
say "  ✓ tunnel URL=$URL"

# ── 3: webhook rewrite ─────────────────────────────────────────────────────
say "Phase 3: point gitlab.com hook → ${URL}/webhook"
curl -sf -m5 -X PUT "$GITLAB_API/projects/$PROJ_ENC/hooks/$HOOK_ID" \
  -H "PRIVATE-TOKEN: $TOK" -H "Content-Type: application/json" \
  -d "{\"url\":\"${URL}/webhook\",\"pipeline_events\":true,\"push_events\":false}" \
  >/dev/null \
  || abort "webhook PUT failed (check HOOK_ID=$HOOK_ID exists on $PROJECT_PATH)" 2
say "  ✓ webhook rewritten"

# ── 4: smoke ───────────────────────────────────────────────────────────────
say "Phase 4: smoke (timeout ${TIMEOUT}s)"
TIMEOUT="$TIMEOUT" PROJECT_PATH="$PROJECT_PATH" NAMESPACE="$NAMESPACE" \
  bash "$REPO_DIR/infra/k8s/gitlab-smoke.sh"
SMOKE_RC=$?

case "$SMOKE_RC" in
  0)  say "=== PASS ==="; exit 0 ;;
  3)  say "=== TIMEOUT ==="; exit 3 ;;
  *)  say "=== FAIL (rc=$SMOKE_RC) ==="; exit 2 ;;
esac
