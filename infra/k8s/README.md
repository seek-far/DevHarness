# SDLCMA on Kubernetes (kind) → gitlab.com

Local deployment path: the existing Helm chart in `infra/helm/sdlcma/` running
on a `kind` cluster against **gitlab.com**, with **cloudflared** providing the
webhook ingress (no public IP required).

This is the K8s sibling of `infra/aws-ecs/` — same `gitlab_saas` worker env,
same cloudflared trick, different orchestration target. The decoupling is via
`WORKER_SPAWNER=k8s` (project invariant #1; tests pin this).

## Prereqs

- `docker` (Docker Desktop with WSL integration is fine), `kind`, `helm`,
  `kubectl` on PATH.
- `settings/worker_gitlab_saas.env` populated with at least:
  - `GITLAB_PRIVATE_TOKEN=glpat-…`  (gitlab.com Project/Group token,
    scopes: `api` + `write_repository`)
  - `LLM_API_KEY=…`
- A gitlab.com test repo whose `main` branch reliably fails a pipeline
  (a one-file pytest off-by-one is enough), with a project webhook **already
  created** — the regression script rewrites its URL on-the-fly and restores
  it afterwards. Default `HOOK_ID=78655514` matches the existing AWS ECS
  test setup on `lishu20161/order_be`; override `HOOK_ID` / `PROJECT_PATH`
  for a different repo.

## Quick start

```bash
# bring stack up (kind + build + load + helm + cloudflared)
bash infra/k8s/setup.sh

# the URL is printed at the end; point your gitlab.com project webhook at
#   <url>/webhook  (Pipeline + Job events)
# then either trigger a pipeline manually, or:
PROJECT_PATH=lishu20161/order_be bash infra/k8s/gitlab-smoke.sh

# tear down
bash infra/k8s/teardown.sh
```

## One-command regression

```bash
bash infra/k8s/regression.sh                 # full setup → smoke → teardown
bash infra/k8s/regression.sh --no-teardown   # leave running for diagnosis
bash infra/k8s/regression.sh --keep-cluster  # uninstall release but keep kind
```

The regression captures the project's pre-existing webhook URL and restores
it on exit (success, failure, or trap), so it plays nicely with other
deployment paths (e.g. AWS ECS) sharing the same `HOOK_ID`.

## What the stack looks like

```
kind cluster (sdlcma-dev)
└── namespace: sdlcma
    ├── Deployment redis        (1 replica, PVC for stream durability)
    ├── Deployment gateway      (1 replica, Service ClusterIP :8000)
    ├── Deployment orchestrator (1 replica, ServiceAccount + RBAC for Jobs)
    ├── Deployment cloudflared  (1 replica, dials out → trycloudflare URL)
    └── Job bf-worker-<slug>    (one per bug, ephemeral, TTL self-GC)
```

- **No registry**: images are `kind load`-ed; chart sets
  `imagePullPolicy: IfNotPresent`.
- **Worker spawner**: `WORKER_SPAWNER=k8s` (set in the overlay) →
  `K8sJobSpawner` creates a `bf-worker-<bug-slug>` Job per bug via the
  in-cluster BatchV1Api. `backoff_limit=0` (orchestrator owns retries),
  `automount_service_account_token=False`, `BF_CHECKPOINT_BACKEND=none`
  (ephemeral, invariant #4).
- **Webhook ingress**: cloudflared opens an outbound tunnel to Cloudflare's
  edge and returns a `https://<random>.trycloudflare.com` URL. The URL
  changes on each pod restart — fine for dev, point a named tunnel at
  `http://gateway:8000` if you need stability.

## Common operations

```bash
# tail orchestrator
kubectl -n sdlcma logs deploy/orchestrator -f

# watch jobs
kubectl -n sdlcma get jobs -l app=bf-worker -w

# get the current cloudflared URL
kubectl -n sdlcma logs deploy/cloudflared --tail=100 | grep trycloudflare

# roll a config change without a rebuild
helm upgrade sdlcma infra/helm/sdlcma -n sdlcma \
  -f infra/helm/sdlcma/values-gitlab-saas.yaml --set namespace.create=false
kubectl -n sdlcma rollout restart deployment/orchestrator
```

## Files

| File | Purpose |
|---|---|
| `setup.sh` | Idempotent: kind cluster + image build + kind load + helm install + URL extract |
| `teardown.sh` | helm uninstall + kind delete (`--keep-cluster` / `--keep-secret` opt-outs) |
| `gitlab-smoke.sh` | Trigger a pipeline on gitlab.com, watch Jobs, verify a new `auto/bf/*` MR |
| `regression.sh` | setup + webhook-rewrite + smoke + restore — one shot |

The Helm chart and the kind cluster config it depends on live elsewhere:

| Asset | Path |
|---|---|
| Helm chart | `infra/helm/sdlcma/` |
| gitlab.com overlay | `infra/helm/sdlcma/values-gitlab-saas.yaml` |
| kind cluster config | `infra/kind/cluster.yaml` |
| Worker env (token, model) | `settings/worker_gitlab_saas.env` |
