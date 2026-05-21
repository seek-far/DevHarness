#!/usr/bin/env bash
# ============================================================================
# infra/public-host/regression.sh
#
# One-command regression for the cloud Stage 2 path on a public-IP host
# (IONOS 82.165.48.174 by default): containerized stack against gitlab.com,
# webhook over cloudflared. Phases:
#   1 env check    (deps + SSH reachable + co-tenant headroom)
#   2 version det. (compare local image ID vs remote image ID via SSH)
#   3 update       (FORCE_SHIP setup.sh if stale OR if stack down)
#   4 setup        (setup.sh; idempotent; brings up swap + stack + tunnel)
#   5 smoke        (extract trycloudflare URL, PUT webhook on gitlab.com,
#                   trigger pipeline, poll for new MR)
#   6 restore      (teardown.sh; revert webhook)
#
# Exit codes: 0 PASS / 2 FAIL / 3 TIMEOUT / 4 PRE-FLIGHT FAIL
#
# Usage:
#   bash infra/public-host/regression.sh \
#     [--timeout 600] [--no-update] [--no-teardown] [--keep-env]
# ============================================================================
set -uo pipefail

START_TS=$(date +%s)
say()   { printf '[t+%4ds] %s\n' $(($(date +%s) - START_TS)) "$*"; }
abort() { echo; echo "ABORT: $1" >&2; exit "${2:-4}"; }

TIMEOUT=600
NO_UPDATE=0
NO_TEARDOWN=0
KEEP_ENV=0
while [ $# -gt 0 ]; do
  case "$1" in
    --timeout)     TIMEOUT="$2"; shift 2 ;;
    --no-update)   NO_UPDATE=1;   shift ;;
    --no-teardown) NO_TEARDOWN=1; shift ;;
    --keep-env)    KEEP_ENV=1;    shift ;;
    -h|--help)     sed -n '4,20p' "$0"; exit 0 ;;
    *) abort "unknown arg: $1" 4 ;;
  esac
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOST="${HOST:-82.165.48.174}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/sales_deploy}"
SSH_USER="${SSH_USER:-root}"
SSH="ssh -i ${SSH_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=12 ${SSH_USER}@${HOST}"
GITLAB_API="https://gitlab.com/api/v4"
PROJ_PATH="lishu20161/order_be"
PROJ_ENC="lishu20161%2Forder_be"
HOOK_ID=78655514   # the long-lived hook on this gitlab.com project
IMAGES=(dh-gateway:latest dh-orchestrator:latest dh-bf-worker:latest)
cd "$REPO_DIR"

# ── Phase 1: environment check ──────────────────────────────────────────────
say "Phase 1: environment check"
for c in docker curl python3 ssh; do
  command -v "$c" >/dev/null || abort "missing dep: $c"
done
[ -f "$SSH_KEY" ] || abort "ssh key not found: $SSH_KEY"
[ -f "$REPO_DIR/settings/worker_gitlab_saas.env" ] \
  || abort "settings/worker_gitlab_saas.env missing (gitignored — populate from .example)"
TOK=$(grep -oE 'GITLAB_PRIVATE_TOKEN=.+' \
   "$REPO_DIR/settings/worker_gitlab_saas.env" | cut -d= -f2-)
[ -n "$TOK" ] || abort "GITLAB_PRIVATE_TOKEN missing"
$SSH 'echo ok' >/dev/null 2>&1 || abort "ssh to $HOST failed (key/firewall?)"
# Co-tenant rule (project memory project_public_host_phase05): sales-retro
# should be down to leave RAM headroom on the 2-GB box.
SALES_UP=$($SSH 'docker ps --format "{{.Names}}" 2>/dev/null | grep -ciE "^sales|caddy" || true')
if [ "${SALES_UP:-0}" -gt 1 ]; then
  say "  WARN: $SALES_UP sales-retro/caddy containers up — consider sales_02/deploy/teardown.sh"
else
  say "  ✓ co-tenant footprint small ($SALES_UP container)"
fi
say "  ✓ deps + SSH + token"

# ── Phase 2: version detection ──────────────────────────────────────────────
say "Phase 2: version detection"
LOCAL_IDS=$(for img in "${IMAGES[@]}"; do docker image inspect "$img" --format '{{.Id}}' 2>/dev/null; done | sort)
[ "$(echo "$LOCAL_IDS" | wc -l)" -eq 3 ] || abort "one of ${IMAGES[*]} missing locally — build first"
REMOTE_IDS=$($SSH "for img in ${IMAGES[*]}; do docker image inspect \$img --format '{{.Id}}' 2>/dev/null; done | sort" 2>/dev/null || true)
NEEDS_SHIP=0
if [ "$LOCAL_IDS" != "$REMOTE_IDS" ]; then
  NEEDS_SHIP=1
  say "  remote images differ from local — will ship"
else
  say "  ✓ remote image IDs match local"
fi
STACK_UP=$($SSH 'docker ps --format "{{.Names}}" 2>/dev/null | grep -c sdlcma-stack || true')
[ "${STACK_UP:-0}" -ge 4 ] && say "  ✓ stack already up ($STACK_UP containers)" || { NEEDS_SHIP=1; say "  stack down — setup needed"; }

# ── Webhook capture for restore ─────────────────────────────────────────────
PRIOR_HOOK=$(curl -sf -m5 "$GITLAB_API/projects/$PROJ_ENC/hooks/$HOOK_ID" \
   -H "PRIVATE-TOKEN: $TOK" \
   | python3 -c "import json,sys;print(json.load(sys.stdin).get('url',''))" 2>/dev/null || echo "")

restore() {
  local rc=$?
  set +e
  if [ "$NO_TEARDOWN" = 0 ]; then
    say "Phase 6: restore"
    bash "$REPO_DIR/infra/public-host/teardown.sh" >/dev/null 2>&1 \
      && say "  ✓ stack down + swap removed" || say "  WARN: teardown non-zero"
  else
    say "Phase 6: --no-teardown → stack left running (trycloudflare URL is ephemeral)"
  fi
  if [ "$KEEP_ENV" = 0 ] && [ -n "$PRIOR_HOOK" ]; then
    curl -sf -m5 -X PUT "$GITLAB_API/projects/$PROJ_ENC/hooks/$HOOK_ID" \
      -H "PRIVATE-TOKEN: $TOK" -H "Content-Type: application/json" \
      -d "{\"url\":\"$PRIOR_HOOK\",\"pipeline_events\":true,\"push_events\":false}" \
      >/dev/null && say "  ✓ webhook restored → $PRIOR_HOOK"
  fi
  exit "$rc"
}
trap restore EXIT

# ── Phase 3+4: update + setup (setup.sh is idempotent + handles both) ──────
say "Phase 3+4: setup ($([ "$NEEDS_SHIP" = 1 ] && echo 'FORCE_SHIP=1' || echo 'no re-ship'))"
if [ "$NO_UPDATE" = 1 ] && [ "$NEEDS_SHIP" = 1 ]; then
  say "  WARN: --no-update but images appear stale; proceeding without ship"
fi
if [ "$NEEDS_SHIP" = 1 ] && [ "$NO_UPDATE" = 0 ]; then
  FORCE_SHIP=1 bash "$REPO_DIR/infra/public-host/setup.sh" 2>&1 \
    | grep -E "^(===|  )" | sed 's/^/  /' \
    | tail -40
else
  bash "$REPO_DIR/infra/public-host/setup.sh" 2>&1 \
    | grep -E "^(===|  )" | sed 's/^/  /' \
    | tail -40
fi

# ── Wait for cloudflared URL, configure webhook ─────────────────────────────
say "Phase 4: extract cloudflared URL"
URL=""
for i in $(seq 1 24); do   # ~2 min
  URL=$($SSH 'cd /root/sdlcma-stack && docker compose -f docker-compose.yml -f docker-compose.public-host.yml logs cloudflared 2>&1 | grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" | head -1' 2>/dev/null)
  [ -n "$URL" ] && break
  sleep 5
done
[ -n "$URL" ] || abort "cloudflared URL not visible after 2 min" 2
say "  ✓ URL=$URL"
curl -sf -m5 -X PUT "$GITLAB_API/projects/$PROJ_ENC/hooks/$HOOK_ID" \
  -H "PRIVATE-TOKEN: $TOK" -H "Content-Type: application/json" \
  -d "{\"url\":\"${URL}/webhook\",\"pipeline_events\":true,\"push_events\":false}" \
  >/dev/null
say "  ✓ webhook → ${URL}/webhook"

# ── Phase 5: smoke ──────────────────────────────────────────────────────────
say "Phase 5: smoke (timeout ${TIMEOUT}s)"
BASELINE=$(curl -sf -m5 "$GITLAB_API/projects/$PROJ_ENC/merge_requests?per_page=1&order_by=created_at&sort=desc" \
   -H "PRIVATE-TOKEN: $TOK" \
   | python3 -c "import json,sys;mrs=json.load(sys.stdin);print(mrs[0]['iid'] if mrs else 0)")
say "  baseline MR iid = $BASELINE"
PIPE=$(curl -sf -m5 -X POST "$GITLAB_API/projects/$PROJ_ENC/pipeline?ref=main" \
   -H "PRIVATE-TOKEN: $TOK" \
   | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('id',''))")
[ -n "$PIPE" ] || abort "pipeline trigger failed" 2
say "  triggered pipeline $PIPE"

POLL_START=$(date +%s)
RESULT=TIMEOUT
while [ $(($(date +%s) - POLL_START)) -lt "$TIMEOUT" ]; do
  NEW_MR=$(curl -sf -m5 "$GITLAB_API/projects/$PROJ_ENC/merge_requests?state=opened&order_by=created_at&sort=desc&per_page=3" \
     -H "PRIVATE-TOKEN: $TOK" \
     | python3 -c "
import json, sys
mrs = json.load(sys.stdin)
new = [m for m in mrs if m['iid'] > $BASELINE and m['source_branch'].startswith('auto/bf/')]
print('!{} {}'.format(new[0]['iid'], new[0]['source_branch'])) if new else print('')
")
  if [ -n "$NEW_MR" ]; then
    RESULT=PASS
    say "  ✓ MR opened: $NEW_MR"
    break
  fi
  WT=$($SSH 'docker ps --filter "name=dh-bf-worker-" --format "{{.Names}}" 2>/dev/null | wc -l')
  ELAPSED=$(($(date +%s) - POLL_START))
  say "  [...] worker containers on host=${WT} (t+${ELAPSED}s)"
  sleep 20
done

echo
case "$RESULT" in
  PASS)    say "=== PASS ==="; exit 0 ;;
  TIMEOUT) say "=== TIMEOUT (no new auto/bf MR within ${TIMEOUT}s) ==="; exit 3 ;;
esac
