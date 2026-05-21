#!/usr/bin/env bash
# ============================================================================
# infra/aws-ecs/regression.sh
#
# One-command regression for AWS ECS deployment. Phases:
#   1 env check    (deps + aws-cli + creds + stack existence)
#   2 version det. (compare local image digest vs ECR :latest digest)
#   3 update       (deploy-images.sh if stale)
#   4 setup        (scale service to 1 if it's at 0; wait services-stable;
#                   extract cloudflared URL; PUT webhook)
#   5 smoke        (trigger pipeline → poll for new MR)
#   6 restore      (scale service back to 0 by default; revert webhook;
#                   delete-stack only if --teardown=full)
#
# Defaults assume the stack already exists (created via create-stack.sh once).
# If absent, the script exits 4 and prints how to create it.
#
# Exit codes: 0 PASS / 2 FAIL / 3 TIMEOUT / 4 PRE-FLIGHT FAIL
#
# Usage:
#   bash infra/aws-ecs/regression.sh \
#     [--timeout 900] [--no-update] [--no-teardown | --teardown=full] [--keep-env]
#
# REGION/STACK_NAME can be overridden via env vars (default eu-north-1,
# sdlcma-stack).
# ============================================================================
set -uo pipefail

START_TS=$(date +%s)
say()   { printf '[t+%4ds] %s\n' $(($(date +%s) - START_TS)) "$*"; }
abort() { echo; echo "ABORT: $1" >&2; exit "${2:-4}"; }

TIMEOUT=900
NO_UPDATE=0
TEARDOWN_MODE=scale_zero     # scale_zero | full | none
KEEP_ENV=0
while [ $# -gt 0 ]; do
  case "$1" in
    --timeout)        TIMEOUT="$2"; shift 2 ;;
    --no-update)      NO_UPDATE=1; shift ;;
    --no-teardown)    TEARDOWN_MODE=none; shift ;;
    --teardown=full)  TEARDOWN_MODE=full; shift ;;
    --teardown=scale) TEARDOWN_MODE=scale_zero; shift ;;
    --keep-env)       KEEP_ENV=1; shift ;;
    -h|--help)        sed -n '4,25p' "$0"; exit 0 ;;
    *) abort "unknown arg: $1" 4 ;;
  esac
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REGION="${REGION:-eu-north-1}"
STACK_NAME="${STACK_NAME:-sdlcma-stack}"
CLUSTER="${CLUSTER:-sdlcma-cluster}"
SERVICE="${SERVICE:-sdlcma-services}"
GITLAB_API="https://gitlab.com/api/v4"
PROJ_PATH="lishu20161/order_be"
PROJ_ENC="lishu20161%2Forder_be"
HOOK_ID=78655514
IMAGES=(dh-gateway dh-orchestrator dh-bf-worker)
cd "$REPO_DIR"

# ── Phase 1: environment check ──────────────────────────────────────────────
say "Phase 1: environment check"
for c in docker aws curl python3; do
  command -v "$c" >/dev/null || abort "missing dep: $c"
done
docker info >/dev/null 2>&1 || abort "docker daemon unreachable"
aws sts get-caller-identity >/dev/null 2>&1 || abort "aws creds not configured"
ACCT=$(aws sts get-caller-identity --query Account --output text)
ECR="${ACCT}.dkr.ecr.${REGION}.amazonaws.com"
STACK_STATUS=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK_NAME" \
   --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo MISSING)
[ "$STACK_STATUS" = MISSING ] \
  && abort "stack $STACK_NAME not present in $REGION — create via infra/aws-ecs/create-stack.sh"
[ "$STACK_STATUS" = CREATE_COMPLETE ] || [ "$STACK_STATUS" = UPDATE_COMPLETE ] \
  || abort "stack in unexpected state: $STACK_STATUS"
TOK=$(grep -oE 'GITLAB_PRIVATE_TOKEN=.+' \
   "$REPO_DIR/settings/worker_gitlab_saas.env" | cut -d= -f2-)
[ -n "$TOK" ] || abort "GITLAB_PRIVATE_TOKEN missing from worker_gitlab_saas.env"
say "  ✓ deps + aws creds + stack=$STACK_STATUS + token"

# ── Phase 2: version detection ──────────────────────────────────────────────
say "Phase 2: version detection (local image vs ECR :latest digest)"
NEEDS_PUSH=0
for img in "${IMAGES[@]}"; do
  LOCAL_DIGEST=$(docker image inspect "$img:latest" --format '{{join .RepoDigests "\n"}}' 2>/dev/null \
    | grep -oE "$ECR/$img@sha256:[a-f0-9]+" | head -1 | cut -d@ -f2)
  REMOTE_DIGEST=$(aws ecr describe-images --region "$REGION" --repository-name "$img" \
    --image-ids imageTag=latest \
    --query 'imageDetails[0].imageDigest' --output text 2>/dev/null || echo "")
  if [ -z "$LOCAL_DIGEST" ] || [ "$LOCAL_DIGEST" != "$REMOTE_DIGEST" ]; then
    NEEDS_PUSH=1
    say "  $img:  local=${LOCAL_DIGEST:-MISSING}  ecr=${REMOTE_DIGEST:-MISSING}  → push"
  else
    say "  ✓ $img digest matches ECR"
  fi
done

# ── Capture pre-state for restore ───────────────────────────────────────────
DESIRED_BEFORE=$(aws ecs describe-services --region "$REGION" --cluster "$CLUSTER" \
   --services "$SERVICE" --query 'services[0].desiredCount' --output text 2>/dev/null || echo 0)
PRIOR_HOOK=$(curl -sf -m5 "$GITLAB_API/projects/$PROJ_ENC/hooks/$HOOK_ID" \
   -H "PRIVATE-TOKEN: $TOK" \
   | python3 -c "import json,sys;print(json.load(sys.stdin).get('url',''))" 2>/dev/null || echo "")

restore() {
  local rc=$?
  set +e
  case "$TEARDOWN_MODE" in
    full)
      say "Phase 6: restore — delete-stack"
      bash "$REPO_DIR/infra/aws-ecs/delete-stack.sh" 2>&1 | tail -8 | sed 's/^/  /' \
        && say "  ✓ stack deleted"
      ;;
    scale_zero)
      say "Phase 6: restore — scale service to 0 (default; cheapest pause)"
      aws ecs update-service --region "$REGION" --cluster "$CLUSTER" \
         --service "$SERVICE" --desired-count 0 \
         --query 'service.desiredCount' --output text >/dev/null \
        && say "  ✓ desiredCount=0 (EC2 still up; free-tier hours)"
      ;;
    none)
      say "Phase 6: --no-teardown → service left at desiredCount=$DESIRED_BEFORE"
      ;;
  esac
  if [ "$KEEP_ENV" = 0 ] && [ -n "$PRIOR_HOOK" ]; then
    curl -sf -m5 -X PUT "$GITLAB_API/projects/$PROJ_ENC/hooks/$HOOK_ID" \
      -H "PRIVATE-TOKEN: $TOK" -H "Content-Type: application/json" \
      -d "{\"url\":\"$PRIOR_HOOK\",\"pipeline_events\":true,\"push_events\":false}" \
      >/dev/null && say "  ✓ webhook restored → $PRIOR_HOOK"
  fi
  exit "$rc"
}
trap restore EXIT

# ── Phase 3: push images if stale ──────────────────────────────────────────
say "Phase 3: update if stale"
if [ "$NO_UPDATE" = 1 ]; then
  say "  --no-update: skip"
elif [ "$NEEDS_PUSH" = 1 ]; then
  REGION="$REGION" bash "$REPO_DIR/infra/aws-ecs/deploy-images.sh" 2>&1 \
    | grep -E "^(===|Pushed|latest:)" | tail -20 | sed 's/^/  /'
  say "  ✓ images pushed"
else
  say "  current digests match ECR — skip"
fi

# ── Phase 4: scale up + wait stable + grab tunnel URL + webhook ────────────
say "Phase 4: setup (scale to 1, wait stable, extract URL)"
aws ecs update-service --region "$REGION" --cluster "$CLUSTER" --service "$SERVICE" \
   --desired-count 1 --force-new-deployment \
   --query 'service.desiredCount' --output text >/dev/null
say "  service desiredCount=1, force-new-deployment"
aws ecs wait services-stable --region "$REGION" --cluster "$CLUSTER" --services "$SERVICE"
say "  ✓ services-stable"

TASK_ID=$(aws ecs list-tasks --region "$REGION" --cluster "$CLUSTER" --service-name "$SERVICE" \
   --query 'taskArns[0]' --output text | xargs -I{} basename {})
[ -n "$TASK_ID" ] && [ "$TASK_ID" != "None" ] || abort "no task running" 2

URL=""
for i in $(seq 1 30); do  # ~2.5 min
  URL=$(aws logs get-log-events --region "$REGION" \
     --log-group-name /sdlcma/services \
     --log-stream-name "cloudflared/cloudflared/$TASK_ID" \
     --limit 50 --output text --query 'events[].message' 2>/dev/null \
     | tr -d '\r' | grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" | sort -u | tail -1)
  [ -n "$URL" ] && break
  sleep 5
done
[ -n "$URL" ] || abort "cloudflared URL not visible after 2.5 min" 2
say "  ✓ tunnel URL=$URL"

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
  WT=$(aws ecs list-tasks --region "$REGION" --cluster "$CLUSTER" --family bf-worker \
       --output text 2>/dev/null | grep -c 'task/')
  ELAPSED=$(($(date +%s) - POLL_START))
  say "  [...] bf-worker tasks=${WT} (t+${ELAPSED}s)"
  sleep 20
done

echo
case "$RESULT" in
  PASS)    say "=== PASS ==="; exit 0 ;;
  TIMEOUT) say "=== TIMEOUT (no new auto/bf MR within ${TIMEOUT}s) ==="; exit 3 ;;
esac
