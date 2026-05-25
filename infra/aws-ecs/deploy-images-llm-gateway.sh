#!/usr/bin/env bash
# ============================================================================
# Build and push SDLCMA images to ECR — gateway variant.
#
# Superset of `deploy-images.sh`: builds the same 3 images PLUS the new
# `dh-llm-gateway` image used only by `stack-llm-gateway.yml`. Safe to run
# even when the stack in flight is the non-gateway one (the extra image just
# sits in its ECR repo unused).
#
# Prerequisites:
#   1. AWS CLI installed and configured (aws configure)
#   2. Docker running locally
#   3. ECR repos already created (via `stack-llm-gateway.yml` create-stack —
#      that template also creates dh-llm-gateway with DeletionPolicy: Retain)
#
# Usage:
#   REGION=eu-north-1 bash infra/aws-ecs/deploy-images-llm-gateway.sh
#
# This builds all 4 images locally, tags them for ECR, and pushes.
# ============================================================================
set -euo pipefail

REGION="${REGION:-eu-north-1}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# On WSL, Docker Desktop's CLI is `docker.exe`. Honour $DOCKER (e.g.
# DOCKER=docker.exe bash deploy-images-llm-gateway.sh) and fall back to
# whichever is on PATH.
DOCKER="${DOCKER:-$(command -v docker 2>/dev/null || command -v docker.exe 2>/dev/null || echo docker)}"

say() { printf '\n=== %s ===\n' "$*"; }

say "preflight: check all 4 ECR repos exist"
MISSING=()
for repo in dh-gateway dh-orchestrator dh-bf-worker dh-llm-gateway; do
  if ! aws ecr describe-repositories --repository-names "$repo" --region "$REGION" >/dev/null 2>&1; then
    MISSING+=("$repo")
  fi
done
if [ "${#MISSING[@]}" -gt 0 ]; then
  cat >&2 <<EOF

ERROR: the following ECR repo(s) do not exist in region $REGION:
  ${MISSING[*]}

These are normally created by 'aws cloudformation create-stack' (or
'update-stack') with infra/aws-ecs/stack-llm-gateway.yml. Run that first,
then re-run this script.

Quick-but-dirty alternative (creates the repo OUTSIDE CloudFormation —
will collide if you later run update-stack with stack-llm-gateway.yml):

  $(for r in "${MISSING[@]}"; do echo "  aws ecr create-repository --repository-name $r --region $REGION"; done)

EOF
  exit 2
fi

say "ECR login (using $DOCKER)"
aws ecr get-login-password --region "$REGION" | \
  "$DOCKER" login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

cd "$REPO_ROOT"

say "Build dh-gateway"
"$DOCKER" build -f Dockerfile.gateway -t dh-gateway:latest .
"$DOCKER" tag dh-gateway:latest "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/dh-gateway:latest"
"$DOCKER" push "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/dh-gateway:latest"

say "Build dh-orchestrator"
"$DOCKER" build -f Dockerfile.orchestrator -t dh-orchestrator:latest .
"$DOCKER" tag dh-orchestrator:latest "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/dh-orchestrator:latest"
"$DOCKER" push "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/dh-orchestrator:latest"

say "Build dh-bf-worker"
"$DOCKER" build -f Dockerfile.bf-worker -t dh-bf-worker:latest .
"$DOCKER" tag dh-bf-worker:latest "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/dh-bf-worker:latest"
"$DOCKER" push "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/dh-bf-worker:latest"

say "Build dh-llm-gateway"
"$DOCKER" build -f Dockerfile.llm-gateway -t dh-llm-gateway:latest .
"$DOCKER" tag dh-llm-gateway:latest "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/dh-llm-gateway:latest"
"$DOCKER" push "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/dh-llm-gateway:latest"

say "Done — all 4 images pushed"
echo "Region:  $REGION"
echo "Account: $ACCOUNT_ID"
