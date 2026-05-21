#!/usr/bin/env bash
# ============================================================================
# End-to-end GitLab-mode smoke test on ECS.
#
# Triggers a failing CI pipeline on the gitlab.com test project, watches
# CloudWatch logs for the orchestrator to pick up the webhook and spawn a
# worker, then verifies the MR is opened.
#
# Prerequisites:
#   1. Stack running (create-stack.sh completed)
#   2. Cloudflared webhook URL configured on gitlab.com project
#   3. Test project whose main branch reproduces a bug
#
# Usage:
#   PROJECT_PATH=grp/buggy \
#   GITLAB_TOKEN=glpat-xxx \
#   bash infra/aws-ecs/gitlab-smoke.sh
# ============================================================================
set -euo pipefail

REGION="${REGION:-us-east-1}"
STACK_NAME="${STACK_NAME:-sdlcma-stack}"
GITLAB_TOKEN="${GITLAB_TOKEN:?must set GITLAB_TOKEN}"
PROJECT_PATH="${PROJECT_PATH:?must set PROJECT_PATH (e.g. user/repo)}"

GITLAB_API="https://gitlab.com/api/v4"
PROJECT_ENCODED="${PROJECT_PATH//\//%2F}"

say() { printf '\n=== %s ===\n' "$*"; }

say "Trigger retry on gitlab.com project: $PROJECT_PATH"
PIPELINE_RESP="$(curl -sSf -X POST \
  "${GITLAB_API}/projects/${PROJECT_ENCODED}/pipeline?ref=main" \
  -H "PRIVATE-TOKEN: ${GITLAB_TOKEN}")"
PIPELINE_ID="$(echo "$PIPELINE_RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
echo "Pipeline ID: $PIPELINE_ID"
echo "Watch: https://gitlab.com/${PROJECT_PATH}/-/pipelines/${PIPELINE_ID}"

say "Watch CloudWatch logs for orchestrator activity (60s timeout)"
START_TS="$(date +%s)"
MAX_WAIT=120
FOUND=0

while [ "$(($(date +%s) - START_TS))" -lt "$MAX_WAIT" ]; do
  LOGS="$(aws logs tail /sdlcma/services --region "$REGION" --since 2m 2>/dev/null || true)"
  if echo "$LOGS" | grep -q "Spawner.*started\|EcsSpawner.*started"; then
    echo "Worker spawned!"
    FOUND=1
    break
  fi
  if echo "$LOGS" | grep -qE "outcome.*fixed|MR. opened|create_merge_request"; then
    echo "MR created / fix completed!"
    FOUND=1
    break
  fi
  sleep 10
done

if [ "$FOUND" = 0 ]; then
  echo "Timeout — check logs manually:"
  echo "  aws logs tail /sdlcma/services --region $REGION"
  echo "  aws logs tail /sdlcma/worker --region $REGION"
fi

say "Verify MR on gitlab.com"
curl -sS "${GITLAB_API}/projects/${PROJECT_ENCODED}/merge_requests?state=opened&per_page=3" \
  -H "PRIVATE-TOKEN: ${GITLAB_TOKEN}" | \
  python3 -c '
import json, sys
mrs = json.load(sys.stdin)
for mr in mrs:
    print(f"  !{mr["iid"]}  {mr["title"]}  {mr["source_branch"]}->{mr["target_branch"]}  {mr["web_url"]}")
'

echo ""
echo "Done. Check the MR web_url above for the fix."
