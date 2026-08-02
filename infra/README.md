# Infrastructure (k8s + Terraform)

Local k8s, Helm chart, and Terraform setup for SDLCMA. Separate from app code
(`bf_worker/`, `gateway/`, `orchestrator/`) — this is deploy infrastructure.

Roadmap follows `1a.1 → 1a.6` (see top-level conversation / task list):

- `1a.1` — manual kind deploy ✅
- `1a.2` — full native manifests (Deployment / Service / ConfigMap / Secret / PVC) ✅
- `1a.3` — spawner → k8s Job refactor ✅
- `1a.4` — Helm chart ✅
- `1a.5` — Terraform `helm_release` orchestration ✅
- `1a.6` — eval on k8s + observability + finishing touches ✅

## Toolchain versions (verified on WSL2 / Ubuntu 22.04.5 / kernel 6.6.114)

| Tool           | Version           | Install                                           |
| -------------- | ----------------- | ------------------------------------------------- |
| Docker Desktop | 28.0.4 (server)   | Windows installer + WSL2 integration              |
| kubectl        | v1.32.2           | apt (system)                                      |
| kind           | v0.27.0           | direct binary → `~/.local/bin`                    |
| Helm           | v3.17.0           | direct binary → `~/.local/bin`                    |
| Terraform      | v1.10.5           | direct binary → `~/.local/bin`                    |

systemd is enabled in `/etc/wsl.conf` (`[boot] systemd=true`).

## Layout (through 1a.3)

```
infra/
├── kind/
│   ├── cluster.yaml                # kind 2-node cluster (1a.1; host ports 18080/18443)
│   └── manifests/                  # raw manifests — kept; `kubectl apply` path + parity baseline
│       ├── configmap.yaml          # gateway-config / orchestrator-config / worker-config
│       ├── secrets.yaml.example    # template for sdlcma-secrets (real values via kubectl create)
│       ├── redis.yaml              # Namespace + Redis + PVC (1Gi, AOF on)
│       ├── gateway.yaml            # Gateway Deployment + Service (envFrom configmap, /healthz)
│       ├── rbac.yaml               # 1a.3: orchestrator SA + namespaced Role/RoleBinding
│       └── orchestrator.yaml       # Orchestrator Deployment + PVC + serviceAccountName
├── k3s/                            # W3: multi-node k3s harness (cross-continent;
│                                   #     setup/teardown/load-image/crossnode-check/regression)
├── helm/sdlcma/                    # 1a.4: Helm chart — drop-in for the raw manifests
│   ├── Chart.yaml                  # version 0.1.0 / appVersion 1a.4
│   ├── values.yaml                 # all knobs; defaults reproduce raw manifests 1:1
│   ├── .helmignore
│   └── templates/
│       ├── _helpers.tpl            # sdlcma.labels / sdlcma.namespace
│       ├── namespace.yaml          # gated on .Values.namespace.create
│       ├── configmap.yaml          # range → 3 CMs, data via toYaml
│       ├── rbac.yaml               # SA + Role + RoleBinding (1a.3 carried over)
│       ├── redis.yaml gateway.yaml orchestrator.yaml
│       ├── networkpolicy.yaml      # 1a.6: deny-ingress + gateway/redis allows
│       ├── ingress.yaml            # 1a.6: host→gateway (ingress-nginx)
│       ├── eval-job.yaml           # 1a.6: opt-in Indexed Job (eval.enabled)
│       └── NOTES.txt
└── terraform/                      # 1a.5: Terraform orchestrates the chart
    ├── versions.tf                 # provider pins (helm ~>2.17, kubernetes ~>2.33)
    ├── providers.tf                # kubernetes + helm via kubeconfig/context
    ├── variables.tf                # kube_context, image tags, helm_wait (default false)
    ├── main.tf                     # kubernetes_namespace + helm_release(../helm/sdlcma)
    ├── outputs.tf
    ├── terraform.tfvars.example
    └── .terraform.lock.hcl         # tracked (pinned providers); state is NOT
```

The raw `kind/manifests/` set is **frozen at its 1a.3 shape** — it does NOT
include the 1a.4 Helm-ization or the 1a.6 additions (NetworkPolicy, Ingress,
Prometheus annotations, eval Job). **From 1a.4 on the Helm chart is the
source of truth** (and 1a.5 Terraform drives the chart); the raw manifests
survive only as the historical `kubectl apply` path and the 1a.3-era parity
baseline. Do not assume they are equivalent to the chart. The Secret is
**not** in the chart — created out of band, referenced by name only,
preserving the "no real credentials in the repo" stance from 1a.2/1a.3.

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

## Stage 1a.3: spawner → k8s Job

With `env == "local_k8s"`, `orchestrator.Orchestrator` selects
`orchestrator.spawner.K8sJobSpawner` instead of the subprocess
`WorkerSpawner`. Per bug it creates one **Job** (image `dh-bf-worker:latest`,
`backoffLimit: 0`, `ttlSecondsAfterFinished: 600`, pod `restartPolicy:
Never`), pulling non-secret config from the `worker-config` ConfigMap and
credentials from `sdlcma-secrets` via `envFrom`, with per-bug values
(`BUG_ID`, `project_id`, `project_web_url`, `job_id`, `REDIS_URL`) injected
as explicit env. Heartbeat is unchanged — the worker still writes the Redis
TTL key and `HealthMonitor` still owns restart (Job retry is disabled on
purpose: `backoffLimit: 0`). `K8sJobProxy` adapts the Job to the
`asyncio.subprocess.Process`-shaped interface the registry/monitor expect
(`pid` = Job name, `returncode` from `read_namespaced_job_status`,
`terminate/kill` = `delete_namespaced_job`).

The orchestrator pod now runs as the `orchestrator` ServiceAccount, bound by
a **namespaced** Role (not ClusterRole) to: `batch/jobs`
create/get/list/watch/delete and `pods` + `pods/log` read.

### Bootstrap delta vs 1a.2

```bash
# dh-orchestrator gained the `kubernetes` client dep → MUST rebuild + reload
docker build -f Dockerfile.orchestrator -t dh-orchestrator:latest .
kind load docker-image dh-orchestrator:latest --name sdlcma-dev
# dh-bf-worker is the image the Jobs run — ensure it is loaded into kind too
docker build -f Dockerfile.bf-worker -t dh-bf-worker:latest .
kind load docker-image dh-bf-worker:latest --name sdlcma-dev

# RBAC must exist BEFORE the orchestrator pod starts (else create_job → 403)
kubectl apply -f infra/kind/manifests/rbac.yaml
kubectl apply -f infra/kind/manifests/orchestrator.yaml
```

### Inspecting worker Jobs

```bash
kubectl get jobs -n sdlcma                       # one bf-worker-<slug> per bug
kubectl get pods -n sdlcma -l app=bf-worker
kubectl logs -n sdlcma job/bf-worker-<slug> -f   # the worker's run output
kubectl describe job -n sdlcma bf-worker-<slug>  # why a pod didn't start
# Finished Jobs self-GC 600s after completion (ttlSecondsAfterFinished).
```

### Common pitfalls (1a.3)

- **`Jobs.batch is forbidden` (403)**: `rbac.yaml` not applied, or applied
  *after* the orchestrator pod started with the default ServiceAccount.
  Apply RBAC, then `kubectl rollout restart deployment/orchestrator -n sdlcma`.
- **Worker pod `ImagePullBackOff`**: `dh-bf-worker:latest` wasn't
  `kind load`-ed. The Job sets `imagePullPolicy: IfNotPresent`; the image
  must already be on the node (same trap as 1a.1).
- **`bug_id` → Job name**: bug_ids contain `_` and `-` (e.g.
  `2026_05_15-12_30_45_3`), illegal/length-bound for k8s names.
  `_k8s_job_name` lowercases, collapses non-alnum to `-`, prefixes
  `bf-worker-`, suffixes `-r<n>` on orchestrator restart, and caps at 63.
- **Job journal is ephemeral**: the worker writes `evaluation/journal/`
  inside the Job pod's own filesystem, which is gone after
  `ttlSecondsAfterFinished`. Persisting it (PVC / sink) is deferred to a
  later stage; capture `kubectl logs` if a run needs post-mortem.
- **Stale Recreate handover**: orchestrator uses `strategy: Recreate`; a new
  orchestrator pod has an empty in-memory `WorkerRegistry` and will not
  re-adopt Jobs spawned by the previous pod (it relies on Redis heartbeat +
  the deterministic `bf-worker-<slug>` name / 409-adopt path).

## Stage 1a.4: Helm chart

`infra/helm/sdlcma/` packages the same Redis + Gateway + Orchestrator +
ConfigMaps + RBAC as the raw manifests. `helm install` is a drop-in for
`kubectl apply -f infra/kind/manifests/`; defaults in `values.yaml` reproduce
the raw manifests 1:1. The `sdlcma-secrets` Secret stays out of band (chart
references it by name only).

### Install / upgrade

```bash
# Secret first (chart never templates it; idempotent to re-run)
kubectl create secret generic sdlcma-secrets -n sdlcma \
  --from-literal=LLM_API_KEY="$LLM_API_KEY" \
  --from-literal=GITLAB_PRIVATE_TOKEN="$GITLAB_PRIVATE_TOKEN" \
  --from-file=SSH_PRIVATE_KEY=$HOME/.ssh/id_ed25519

# Fresh namespace: let the chart render it
helm install sdlcma infra/helm/sdlcma -n sdlcma --create-namespace

# Namespace already exists (e.g. converting from raw manifests): skip the
# Namespace object so install doesn't collide
helm install sdlcma infra/helm/sdlcma -n sdlcma --set namespace.create=false

# Roll a config change without rebuilding an image
helm upgrade sdlcma infra/helm/sdlcma -n sdlcma \
  --set configMaps.orchestrator.HEALTH_CHECK_INTERVAL=15
kubectl rollout restart deployment/orchestrator -n sdlcma

# Pin images to a build instead of :latest (NOTES §9 follow-up)
helm upgrade sdlcma infra/helm/sdlcma -n sdlcma \
  --set gateway.image.tag=$(git rev-parse --short HEAD) \
  --set orchestrator.image.tag=$(git rev-parse --short HEAD)

helm list -n sdlcma
helm uninstall sdlcma -n sdlcma          # leaves the out-of-band Secret + ns
```

### Converting an existing raw-manifest deployment

Helm will not adopt resources it didn't create. Delete the raw-manifest
objects first **but keep the namespace and the Secret** (don't
`kubectl delete -f redis.yaml` — it declares the Namespace and would cascade
the Secret; delete `deployment/redis svc/redis pvc/redis-data` by name
instead), then `helm install ... --set namespace.create=false`.

### Validate without a cluster

```bash
helm lint infra/helm/sdlcma
helm template sdlcma infra/helm/sdlcma | kubectl apply --dry-run=client -f -
pytest tests/test_helm_chart.py          # 11 cases; skips if helm not on PATH
```

### Common pitfalls (1a.4)

- **`Namespace "sdlcma" already exists`** on install: pass
  `--set namespace.create=false` when the ns is managed elsewhere.
- **`rendered manifests contain a resource that already exists`**: a raw
  manifest (or a previous non-Helm apply) still owns that object. Delete the
  conflicting objects (preserving ns + Secret) before installing.
- **Orchestrator `CreateContainerConfigError` / missing creds**: the Secret
  wasn't created — the chart references `sdlcma-secrets` but never creates it.
- **Editing a template but `helm template` shows no change**: you edited
  `values.yaml`'s commented defaults vs. the actual key, or didn't re-run;
  `helm template --debug` prints the merged values.

## Stage 1a.5: Terraform orchestrates the chart

`infra/terraform/` wraps the 1a.4 chart in a reproducible, idempotent
lifecycle. Terraform owns **two** resources: `kubernetes_namespace.sdlcma`
and `helm_release.sdlcma` (chart = `../helm/sdlcma`). The `sdlcma-secrets`
Secret is deliberately **not** Terraform-managed — same out-of-band stance as
1a.2–1a.4 (no credentials in tfstate). Consequence: the orchestrator pod is
intentionally not Ready until you create the Secret, so `helm_wait` defaults
to **false** (apply returns once Helm reports `deployed`; pods self-heal once
the Secret exists).

### Usage

```bash
cd infra/terraform
terraform init                 # downloads pinned providers, writes the lock file
terraform apply                # creates namespace + helm release

# Create the out-of-band Secret (TF never manages it)
kubectl create secret generic sdlcma-secrets -n sdlcma \
  --from-literal=LLM_API_KEY="$LLM_API_KEY" \
  --from-literal=GITLAB_PRIVATE_TOKEN="$GITLAB_PRIVATE_TOKEN" \
  --from-file=SSH_PRIVATE_KEY=$HOME/.ssh/id_ed25519

terraform plan                 # idempotent: "No changes" after a clean apply
terraform destroy              # removes release + namespace (cascades the Secret)

# Pin images instead of :latest (NOTES §9 follow-up)
terraform apply -var gateway_image_tag=$(git rev-parse --short HEAD) \
                -var orchestrator_image_tag=$(git rev-parse --short HEAD)
```

### Converting an existing Helm-managed deployment

Terraform won't adopt a release/namespace it didn't create. Tear the old one
down first (back up the out-of-band Secret, it gets cascaded):
`helm uninstall sdlcma -n sdlcma` → `kubectl delete ns sdlcma` →
`terraform apply` → recreate the Secret.

### Validate without a cluster

```bash
terraform -chdir=infra/terraform fmt -check -recursive
terraform -chdir=infra/terraform validate     # needs `init` (providers)
pytest tests/test_terraform_config.py          # 7 cases; skips if terraform absent
```

### Common pitfalls (1a.5)

- **`namespaces "sdlcma" already exists`** on apply: a non-Terraform ns is
  present. Either import it (`terraform import kubernetes_namespace.sdlcma
  sdlcma`) or delete it first.
- **`apply` hangs**: you set `helm_wait=true` without the Secret present —
  the orchestrator never becomes Ready. Create the Secret, or keep
  `helm_wait=false`.
- **Lock file churn**: commit `.terraform.lock.hcl`; never commit
  `.terraform/` or `*.tfstate*` (gitignored). Re-run `terraform init` after
  changing provider pins.
- **Three deploy paths now exist** (raw `kubectl apply`, `helm install`,
  `terraform apply`) — they target the same namespace and are mutually
  exclusive. Pick one per cluster; don't interleave.

## Stage 1a.6: NetworkPolicy / Ingress / observability / eval Job

Four chart additions (chart bumped to `0.2.0`, appVersion `1a.6`), all
toggled via `values.yaml`:

| Feature | Default | Value |
|---|---|---|
| NetworkPolicy (ingress lockdown) | on | `networkPolicy.enabled` |
| Ingress (host → gateway) | on | `ingress.enabled` / `ingress.host` |
| Prometheus scrape annotations | on | `observability.prometheusAnnotations` |
| eval Indexed Job | **off** | `eval.enabled` |

**NetworkPolicy** is ingress-only: `default-deny-ingress` + `allow-gateway-ingress`
(:8000 any source) + `allow-redis-from-app` (:6379 from `app in
{gateway,orchestrator,bf-worker}`). Egress is left open on purpose — a
default-deny egress in kind also kills DNS and the workers' LLM/GitLab/image
pulls. The label set is a contract shared with `K8sJobSpawner._build_job`
(worker Job pods carry `app=bf-worker`); changing one side requires the other.

**Ingress** needs ingress-nginx in the cluster (a prerequisite, not chart-managed):

```bash
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.11.3/deploy/static/provider/kind/deploy.yaml
kubectl wait -n ingress-nginx --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller --timeout=120s
# gateway now reachable on the kind host port — port-forward retired:
curl http://localhost:18080/healthz
```

**eval Job** is template-only: the `dh-bf-worker` image doesn't bundle
`evaluation/`, and the runner doesn't shard by `JOB_COMPLETION_INDEX` yet.
Enable + point at an eval-capable image once one exists:
`--set eval.enabled=true,eval.image.repository=<img>`.

**Deploying via Terraform**: bump `Chart.yaml: version` for any chart change
— the helm provider detects local-chart changes by **version**, not file
content, so without a bump `terraform plan` is a no-op and new resources
never deploy. After a bump, the first `plan` shows an output-only diff
(`chart_version` lags one apply — a helm-provider v2 quirk); a second
`apply` settles it and `plan` is then a true no-op.

### Common pitfalls (1a.6)

- **Wrong kubectl context**: more than one kind cluster may exist; Terraform
  targets `kind-sdlcma-dev` explicitly but ad-hoc `kubectl` uses the current
  context. Always `kubectl --context kind-sdlcma-dev …` when verifying.
- **`curl localhost:18080` flaky during a deploy**: the gateway pod is
  rolling; ingress-nginx briefly has no endpoint. Re-test after
  `kubectl rollout status`.
- **NetworkPolicy “broke” connectivity**: check pod `app=` labels match the
  policy selectors (esp. worker Job pods = `app=bf-worker`); a typo there
  silently drops traffic with no error.
- **`terraform plan` shows perpetual `chart_version` diff**: expected once
  after a chart version bump (computed metadata lags); re-`apply` to settle.
