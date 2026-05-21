#!/usr/bin/env bash
# ============================================================================
# Create the SDLCMA ECS CloudFormation stack.
#
# Prerequisites:
#   1. AWS CLI installed and configured
#   2. EC2 key pair created (Console > EC2 > Key Pairs)
#   3. Default VPC with at least 2 public subnets
#   4. gitlab.com access token (api + write_repository scopes)
#   5. LLM API key
#
# Usage:
#   STACK_NAME=sdlcma-stack \
#   KEY_NAME=my-key \
#   GITLAB_TOKEN=glpat-xxx \
#   LLM_API_KEY=sk-xxx \
#   bash infra/aws-ecs/create-stack.sh
#
# The script looks up your default VPC and the first public subnet
# automatically. Override with VPC_ID / SUBNET_A if needed.
# ============================================================================
set -euo pipefail

REGION="${REGION:-us-east-1}"
STACK_NAME="${STACK_NAME:-sdlcma-stack}"
KEY_NAME="${KEY_NAME:?must set KEY_NAME (EC2 key pair name)}"
GITLAB_TOKEN="${GITLAB_TOKEN:?must set GITLAB_TOKEN (gitlab.com PAT)}"
LLM_API_KEY="${LLM_API_KEY:?must set LLM_API_KEY}"
LLM_API_BASE_URL="${LLM_API_BASE_URL:-https://dashscope-intl.aliyuncs.com/compatible-mode/v1}"
LLM_MODEL="${LLM_MODEL:-qwen3-coder-plus}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.micro}"
SWAP_SIZE_GB="${SWAP_SIZE_GB:-4}"

say() { printf '\n=== %s ===\n' "$*"; }

# Auto-detect VPC and subnets if not provided
if [ -z "${VPC_ID:-}" ]; then
  say "Auto-detecting default VPC"
  VPC_ID="$(aws ec2 describe-vpcs --region "$REGION" \
    --filters Name=isDefault,Values=true \
    --query 'Vpcs[0].VpcId' --output text)"
  echo "VPC: $VPC_ID"
fi

if [ -z "${SUBNET_A:-}" ]; then
  say "Auto-detecting public subnet in VPC"
  SUBNET_A="$(aws ec2 describe-subnets --region "$REGION" \
    --filters "Name=vpc-id,Values=$VPC_ID" \
    --query 'Subnets[0].SubnetId' --output text)"
  echo "Subnet A: $SUBNET_A"
fi

STACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say "Creating CloudFormation stack: $STACK_NAME"
aws cloudformation create-stack \
  --region "$REGION" \
  --stack-name "$STACK_NAME" \
  --template-body "file://${STACK_DIR}/stack.yml" \
  --parameters \
    "ParameterKey=VpcId,ParameterValue=${VPC_ID}" \
    "ParameterKey=PublicSubnetA,ParameterValue=${SUBNET_A}" \
    "ParameterKey=KeyName,ParameterValue=${KEY_NAME}" \
    "ParameterKey=InstanceType,ParameterValue=${INSTANCE_TYPE}" \
    "ParameterKey=SwapSizeGB,ParameterValue=${SWAP_SIZE_GB}" \
    "ParameterKey=ServiceDesiredCount,ParameterValue=0" \
    "ParameterKey=GitlabToken,ParameterValue=${GITLAB_TOKEN}" \
    "ParameterKey=LLMApiKey,ParameterValue=${LLM_API_KEY}" \
    "ParameterKey=LLMApiBaseUrl,ParameterValue=${LLM_API_BASE_URL}" \
    "ParameterKey=LLMModel,ParameterValue=${LLM_MODEL}" \
  --capabilities CAPABILITY_IAM \
  --on-failure DELETE

say "Waiting for stack creation (this takes ~5 min)"
aws cloudformation wait stack-create-complete \
  --region "$REGION" \
  --stack-name "$STACK_NAME"

say "Stack outputs"
aws cloudformation describe-stacks \
  --region "$REGION" \
  --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[*].[OutputKey,OutputValue]' \
  --output table

say "Next steps"
echo "Infrastructure created. The ECS service is scaled to 0 tasks."
echo "Push images, then start the service:"
echo ""
echo "1. Build & push images to ECR (from a host with Docker):"
echo "   REGION=$REGION bash infra/aws-ecs/deploy-images.sh"
echo ""
echo "   If Docker is on Windows Desktop (not WSL), run from PowerShell:"
echo "   aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin ${AWS_ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
echo "   docker build -f Dockerfile.gateway -t dh-gateway:latest ."
echo "   docker tag dh-gateway:latest ${AWS_ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/dh-gateway:latest"
echo "   docker push ${AWS_ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/dh-gateway:latest"
echo "   (repeat for Dockerfile.orchestrator → dh-orchestrator, Dockerfile.bf-worker → dh-bf-worker)"
echo ""
echo "2. Scale ECS service to 1 task:"
echo "   aws ecs update-service --cluster sdlcma-cluster --service sdlcma-services --desired-count 1 --region $REGION"
echo ""
echo "3. Find cloudflared URL (wait ~30s after step 2 for task to start):"
echo "   aws logs tail /sdlcma/services --filter trycloudflare --region $REGION"
echo ""
echo "4. Set webhook on gitlab.com project (Pipeline events):"
echo "   https://<trycloudflare-url>/webhook"
echo ""
echo "5. Trigger a failing pipeline on your test project"
echo "6. SSH to instance:"
echo "   ssh -i ~/.ssh/$KEY_NAME.pem ec2-user@<PublicIp>"
echo ""
echo "Teardown:  bash infra/aws-ecs/delete-stack.sh"
echo "           (delete ECR repos manually if desired)"
