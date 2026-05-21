#!/usr/bin/env bash
# ============================================================================
# Delete the SDLCMA ECS CloudFormation stack.
#
# ECR repos are NOT deleted by default (retained for data safety).
# Set DELETE_ECR=1 to also remove them.
#
# Usage:
#   bash infra/aws-ecs/delete-stack.sh
#   DELETE_ECR=1 bash infra/aws-ecs/delete-stack.sh   # full cleanup
# ============================================================================
set -euo pipefail

REGION="${REGION:-us-east-1}"
STACK_NAME="${STACK_NAME:-sdlcma-stack}"
DELETE_ECR="${DELETE_ECR:-0}"

say() { printf '\n=== %s ===\n' "$*"; }

say "Deleting CloudFormation stack: $STACK_NAME"
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK_NAME"

say "Waiting for deletion (this takes ~3 min)"
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK_NAME"

say "Stack deleted"

if [ "$DELETE_ECR" = 1 ]; then
  say "Deleting ECR repositories"
  for repo in dh-gateway dh-orchestrator dh-bf-worker; do
    aws ecr delete-repository --region "$REGION" --repository-name "$repo" --force 2>/dev/null || \
      echo "  $repo: already gone or not found"
  done
else
  echo "ECR repos retained (set DELETE_ECR=1 to remove)."
  echo "  dh-gateway, dh-orchestrator, dh-bf-worker"
fi

echo "Done. Nothing SDLCMA remains on AWS."
