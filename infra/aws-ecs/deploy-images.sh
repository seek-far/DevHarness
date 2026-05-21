#!/usr/bin/env bash
# ============================================================================
# Build and push SDLCMA images to ECR.
#
# Prerequisites:
#   1. AWS CLI installed and configured (aws configure)
#   2. Docker running locally
#   3. ECR repos already created (via CloudFormation or manually)
#
# Usage:
#   REGION=us-east-1 bash infra/aws-ecs/deploy-images.sh
#
# This builds all 3 images locally, tags them for ECR, and pushes.
# ============================================================================
set -euo pipefail

REGION="${REGION:-us-east-1}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# On WSL, Docker Desktop's CLI is `docker.exe`. The script honours $DOCKER
# (e.g. DOCKER=docker.exe bash deploy-images.sh) and falls back to whichever
# is on PATH.
DOCKER="${DOCKER:-$(command -v docker 2>/dev/null || command -v docker.exe 2>/dev/null || echo docker)}"

say() { printf '\n=== %s ===\n' "$*"; }

# Get ECR login
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

say "Done — all images pushed"
echo "Region:  $REGION"
echo "Account: $ACCOUNT_ID"
