# SDLCMA → AWS ECS (free-tier)

ECS deployment of the containerized SDLCMA stack against gitlab.com.
Single EC2 instance (t3.micro, free tier) running one multi-container ECS
service (redis + gateway + orchestrator + cloudflared, **host network mode**
so they share `localhost`) + one-off bf-worker tasks per bug via
`ecs:RunTask` (awsvpc, isolated per-bug ENI). Reuses `ENV=gitlab_saas`
and the gitlab.com HTTPS + `oauth2:<token>` auth path — only the spawner
differs from the public-host harness (the additive `WORKER_SPAWNER=ecs`
setting). No new `ENV` value, no new worker code path. See
`docs/deployment.md` for the full rationale.

## Prerequisites

1. **AWS account** with admin IAM user
2. **AWS CLI** installed and configured (`aws configure`)
3. **EC2 key pair** created in Console > EC2 > Key Pairs (save the .pem)
4. **gitlab.com** project whose `main` branch reproduces a bug, with `.gitlab-ci.yml`
5. **gitlab.com access token** for that project (scopes: `api` + `write_repository`)
6. **LLM API key** (Dashscope / OpenAI-compatible)

## Deploy

```bash
# 1. Create the stack (services task starts at DesiredCount=0 so step 2 can
#    push images first; only after that do we scale to 1).
KEY_NAME=my-key \
GITLAB_TOKEN=glpat-xxx \
LLM_API_KEY=sk-xxx \
bash infra/aws-ecs/create-stack.sh

# 2. Build and push images to ECR
bash infra/aws-ecs/deploy-images.sh

# 3. Scale the service up (pulls the freshly-pushed images)
aws ecs update-service \
  --cluster sdlcma-cluster \
  --service sdlcma-services \
  --desired-count 1 \
  --region us-east-1

# 4. Read the cloudflared URL
aws logs tail /sdlcma/services --filter trycloudflare --region us-east-1
# → https://<rand>.trycloudflare.com

# 5. Set webhook on gitlab.com:
#    Project > Settings > Webhooks
#    URL: https://<rand>.trycloudflare.com/webhook
#    Events: Pipeline events
#    SSL verification: ON (cloudflared provides valid certs)
```

## Smoke test

```bash
PROJECT_PATH=user/repo GITLAB_TOKEN=glpat-xxx \
bash infra/aws-ecs/gitlab-smoke.sh
```

## Teardown

```bash
# Delete everything except ECR repos
bash infra/aws-ecs/delete-stack.sh

# Full cleanup including ECR images
DELETE_ECR=1 bash infra/aws-ecs/delete-stack.sh
```

## Architecture

```
gitlab.com ──webhook──> cloudflared (tunnel) ──> gateway:8000
                                                    │
                                              orchestrator ──> ecs:RunTask
                                                    │               │
                                               redis:6379    bf-worker task
```

## Cost

| Resource | Monthly (free tier) |
|---|---|
| EC2 t3.micro | 750 hrs → $0 |
| EBS 8 GB gp3 | 30 GB free → $0 |
| ECR 750 MB | 500 MB free → ~$0.10 overage |
| CloudWatch 5 GB | 5 GB free → $0 |
| **Total** | **~$0/mo** |

## Troubleshooting

```bash
# Service not starting?
aws ecs describe-services --cluster sdlcma-cluster --services sdlcma-services

# Task failing?
aws ecs describe-tasks --cluster sdlcma-cluster --tasks <task-arn>

# SSH to instance
ssh -i ~/.ssh/<key>.pem ec2-user@<public-ip>

# View container logs (inside instance)
docker ps
docker logs <container-id>

# ECS agent logs (inside instance)
sudo journalctl -u ecs -f
```
