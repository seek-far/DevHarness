# Infrastructure (k8s + Terraform)

Local k8s, Helm chart, and Terraform setup for SDLCMA. Separate from app code
(`bf_worker/`, `gateway/`, `orchestrator/`) — this is deploy infrastructure.

Roadmap follows `1a.1 → 1a.6` (see top-level conversation / task list):

- `1a.1` — manual kind deploy (this stage)
- `1a.2` — full native manifests (Deployment / Service / ConfigMap / Secret / PVC)
- `1a.3` — spawner → k8s Job refactor
- `1a.4` — Helm chart
- `1a.5` — Terraform `helm_release` orchestration
- `1a.6` — eval on k8s + observability + finishing touches

## Toolchain versions (verified on WSL2 / Ubuntu 22.04.5 / kernel 6.6.114)

| Tool           | Version           | Install                                           |
| -------------- | ----------------- | ------------------------------------------------- |
| Docker Desktop | 28.0.4 (server)   | Windows installer + WSL2 integration              |
| kubectl        | v1.32.2           | apt (system)                                      |
| kind           | v0.27.0           | direct binary → `~/.local/bin`                    |
| Helm           | v3.17.0           | direct binary → `~/.local/bin`                    |
| Terraform      | v1.10.5           | direct binary → `~/.local/bin`                    |

systemd is enabled in `/etc/wsl.conf` (`[boot] systemd=true`).

## Stage 1a.2 layout

```
infra/kind/
├── cluster.yaml                    # kind 2-node cluster (1a.1)
└── manifests/
    ├── configmap.yaml              # gateway-config / orchestrator-config / worker-config
    ├── secrets.yaml.example        # template for sdlcma-secrets (real values via kubectl create)
    ├── redis.yaml                  # Redis + PVC (1Gi, AOF on)
    ├── gateway.yaml                # Gateway Deployment + Service (envFrom configmap, /healthz HTTP probe)
    └── orchestrator.yaml           # Orchestrator Deployment + PVC (no Service; subprocess spawner)
```

Config injection style: every Deployment uses `envFrom: configMapRef:` + (for
orchestrator) `secretRef:`. Because Pydantic BaseSettings reads process env at
a higher priority than `.env` files, the values in the ConfigMap override
whatever is baked into the image's `gateway/gateway_local_k8s.env` /
`settings/{orchestrator,worker}_local_k8s.env`. Editing the ConfigMap +
`kubectl rollout restart deployment/<name>` rolls a config change without
rebuilding any image.

## Stage 1a.2 first-time bootstrap

```bash
# 1. Cluster (if not already up)
kind create cluster --config infra/kind/cluster.yaml

# 2. Build + load images (gateway has /healthz added in 1a.2 → must rebuild)
docker build -f Dockerfile.gateway      -t dh-gateway:latest      .
docker build -f Dockerfile.orchestrator -t dh-orchestrator:latest .
kind load docker-image dh-gateway:latest      --name sdlcma-dev
kind load docker-image dh-orchestrator:latest --name sdlcma-dev

# 3. ConfigMaps (creates namespace `sdlcma` transitively via redis.yaml; if
#    applying configmap.yaml first, create the namespace manually):
kubectl apply -f infra/kind/manifests/redis.yaml          # also creates the ns
kubectl apply -f infra/kind/manifests/configmap.yaml

# 4. Secret — DO NOT commit real values. Source them from your shell env or
#    paste at the prompt; the YAML form in secrets.yaml.example is reference
#    only.
kubectl create secret generic sdlcma-secrets -n sdlcma \
  --from-literal=LLM_API_KEY="$LLM_API_KEY" \
  --from-literal=GITLAB_PRIVATE_TOKEN="$GITLAB_PRIVATE_TOKEN" \
  --from-file=SSH_PRIVATE_KEY=$HOME/.ssh/id_ed25519

# 5. Workloads
kubectl apply -f infra/kind/manifests/gateway.yaml
kubectl apply -f infra/kind/manifests/orchestrator.yaml

# 6. Wait + verify
kubectl get all,pvc -n sdlcma
kubectl rollout status deployment/redis        -n sdlcma
kubectl rollout status deployment/gateway      -n sdlcma
kubectl rollout status deployment/orchestrator -n sdlcma
```

## Stage 1a.2 common operations

```bash
# Smoke-test the gateway end-to-end (in two shells)
kubectl port-forward svc/gateway 8000:8000 -n sdlcma
curl http://localhost:8000/healthz                              # → {"status":"ok"}
curl -X POST http://localhost:8000/webhook \
     -H 'Content-Type: application/json' -d '{"_marker":"smoke"}'

# Check the stream landed
kubectl exec -n sdlcma deployment/redis -- redis-cli XLEN gateway:stream
kubectl exec -n sdlcma deployment/redis -- redis-cli XREVRANGE gateway:stream + - COUNT 1

# Roll a config change (no rebuild)
kubectl edit configmap orchestrator-config -n sdlcma
kubectl rollout restart deployment/orchestrator -n sdlcma

# Rotate a secret
kubectl delete secret sdlcma-secrets -n sdlcma
kubectl create secret generic sdlcma-secrets -n sdlcma --from-literal=...
kubectl rollout restart deployment/orchestrator -n sdlcma

# Verify Redis persistence: delete the pod and confirm streams survive.
kubectl exec -n sdlcma deployment/redis -- redis-cli XLEN gateway:stream     # → N
kubectl delete pod -n sdlcma -l app=redis
kubectl rollout status deployment/redis -n sdlcma
kubectl exec -n sdlcma deployment/redis -- redis-cli XLEN gateway:stream     # still N

# Teardown
kubectl delete -f infra/kind/manifests/orchestrator.yaml
kubectl delete -f infra/kind/manifests/gateway.yaml
kubectl delete -f infra/kind/manifests/configmap.yaml
kubectl delete secret sdlcma-secrets -n sdlcma
kubectl delete -f infra/kind/manifests/redis.yaml          # cascades the PVC
```

## Common pitfalls (1a.2)

- **PVC stuck Pending** after `redis.yaml` apply: kind ships
  `local-path-provisioner` in `local-path-storage` ns and registers `standard`
  as the default StorageClass. Check `kubectl get storageclass` — if no class
  is marked `(default)`, kind setup is off. Reinstall kind ≥ v0.27.
- **`envFrom` keys vs case sensitivity**: ConfigMap keys are case-sensitive
  and become env-var names verbatim. Pydantic env-var matching is
  case-insensitive by default, but keep ConfigMap keys UPPERCASE to match
  unix env-var convention.
- **Forgot to recreate the Secret** after rotating a credential: the
  orchestrator pod has already cached old values into process env. Always
  `kubectl rollout restart deployment/orchestrator -n sdlcma` after a secret
  change.
- **`secrets.yaml` accidentally committed**: `.gitignore` excludes
  `infra/kind/manifests/secrets.yaml` (the suffix-less form). Only the
  `.example` template should ever be tracked.
