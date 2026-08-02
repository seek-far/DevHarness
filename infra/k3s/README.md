# SDLCMA on k3s (cross-continent, multi-node)

The multi-node deployment path: the same Helm chart as the kind harness,
running on a **real two-machine k3s cluster** whose nodes sit on different
continents, joined over Tailscale.

```
ls4900 (CN)  k3s server + control-plane + every service + all worker load
minus  (DE)  k3s agent — NoSchedule-tainted by default (see "Placement")
```

This is the sibling of `infra/k8s/` (kind, single machine). Both exist; they
share the chart and the smoke script, and **neither modifies the other's
state** — different kubeconfig, different NodePort, different overlay.

Design doc: `/mnt/d/PL/sdlcma/W3-k3s-cluster-design.md` ·
Deep contract + the cross-continent gotchas: `docs/k3s.md`

## Prereqs

- The cluster already exists. Installing k3s is a one-time manual step
  (`docs/k3s.md` §2) — these scripts never install or remove it.
- A dedicated kubeconfig at `~/.kube/k3s.yaml`:
  ```bash
  sudo install -o "$(id -u)" -g "$(id -g)" -m 600 \
    /etc/rancher/k3s/k3s.yaml ~/.kube/k3s.yaml
  ```
  **Not** `~/.kube/config` — see "Why a dedicated kubeconfig" below.
- `settings/worker_local_multi_process.env` with `GITLAB_PRIVATE_TOKEN` and
  `LLM_API_KEY`.
- A GitLab project whose `main` pipeline reliably fails, with **exactly one
  CI job** and a Pipeline webhook pointing at `http://<tailnet-ip>:30800/webhook`.
  The bundled one is `root/k3s-smoke` on ls4900's GitLab.

## Quick start

```bash
bash infra/k3s/setup.sh              # build → import → helm install → report
bash infra/k3s/gitlab-smoke.sh       # trigger a failing pipeline, expect an MR
bash infra/k3s/crossnode-check.sh    # prove a worker can run on minus
bash infra/k3s/teardown.sh           # helm uninstall (cluster untouched)

bash infra/k3s/regression.sh --crossnode   # all of the above, one command
```

## Placement: why minus is tainted

`setup.sh` taints the remote node `sdlcma.io/cross-continent=true:NoSchedule`
before installing the chart.

The reason is not "cross-continent is a bad idea" — it is that
`K8sJobSpawner._build_job` currently emits **no `resources` and no
`nodeSelector`**. To the scheduler a worker Job costs nothing, so a 15.6 GB
WSL2 guest on another continent is an equally good home for it: placement
becomes a coin flip on every webhook. The taint makes the default explicit
and reversible.

**minus is meant to run workers**, and `crossnode-check.sh` proves it can,
today, by dropping the taint for one controlled run. Making it the *default*
is plan item W4, which delivers the three things that make it safe —
`resources`, a `nodeSelector`, and a `node.kubernetes.io/unreachable`
toleration — and removes this taint as part of that change.

`teardown.sh` deliberately does **not** remove the taint (use `--untaint` if
you really want to): re-opening cross-ocean scheduling should be a decision,
not a side effect of uninstalling a release.

## Why a dedicated kubeconfig

`setup.sh` exports `KUBECONFIG=~/.kube/k3s.yaml` and passes `--context`
explicitly. No script here ever runs `kubectl config use-context`.

That is a scar, not a preference. Installing k3s on this host copied its
`k3s.yaml` over `~/.kube/config`, which deleted the kind context — and
`infra/k8s/teardown.sh` guards on that context existing, so it quietly became
a no-op that exits 0 having torn down nothing. Two deployment shapes sharing
one kubeconfig is how that happens. Pinned by `tests/test_k3s_setup_sh.py`.

## Images: there is no `kind load`

Each node runs its own containerd, separate from the host docker daemon. An
image `docker images` can see is invisible to kubelet until imported:

```bash
bash infra/k3s/load-image.sh                 # local node
bash infra/k3s/load-image.sh --node minus    # remote (prints manual steps)
```

Two ways to lose an afternoon:

- **`-n k8s.io` is not optional.** containerd namespaces its image store.
  Import into the default namespace and `ctr images ls` shows the image while
  kubelet still reports `ImagePullBackOff`.
- **Re-importing `:latest` does not restart pods.** kubelet already resolved
  the tag. `setup.sh` rollout-restarts afterwards; if you call `load-image.sh`
  directly, do it yourself.

`sudo` is password-gated on both nodes, so the remote branch **prints** the
commands rather than running them unless `K3S_MINUS_SSH` is set. That printed
path is a first-class route, not a degraded one: a piped `ssh host 'sudo …'`
in an unattended run hangs on an invisible password prompt.

## Webhook ingress: NodePort, not ingress-nginx

k3s has no kind-style `extraPortMappings`, and traefik/servicelb are disabled
because `:80` belongs to a co-tenant GitLab. Standing up a second
ingress-nginx just to renumber a port would also re-import the CN
`@sha256`-digest proxy problem, so the gateway Service is a plain
`NodePort` on **30800** (inside the default 30000-32767 range; 18080 would
need `--service-node-port-range` widened server-side).

## Coexistence with the host's ver99 stack

ls4900 also runs the SWE-bench ver99 stack as plain host subprocesses. Two
orchestrators against one redis share the `orchestrator-group` consumer group
and its PEL, steal each other's pending entries, and **spawn duplicate
workers for the same bug** — silent corruption, not a crash. `setup.sh`
pre-flights this by rendering the merged values and asserting `REDIS_URL`
points at the in-cluster Service, and warns when a host `:8000` listener is
present. Keep the two stacks on disjoint GitLab projects.

## Files

| File | Purpose |
|---|---|
| `setup.sh` | kubeconfig check → deps → isolation pre-flight → cluster health → taint remote → build → import → secret → helm → rollouts → report |
| `teardown.sh` | `helm uninstall` + secret + leftover Jobs. Keeps the taint and the cluster |
| `load-image.sh` | `docker save \| k3s ctr -n k8s.io images import` per node |
| `gitlab-smoke.sh` | Thin wrapper: sets endpoint/project/kubeconfig, execs `infra/k8s/gitlab-smoke.sh` |
| `crossnode-check.sh` | Untaint + cordon → real webhook → assert the worker pod ran on minus → restore + verify |
| `regression.sh` | setup → smoke → (optional crossnode) → teardown |

| Asset | Path |
|---|---|
| Helm chart | `infra/helm/sdlcma/` |
| k3s overlay | `infra/helm/sdlcma/values-k3s-ls4900.yaml` |
| Worker env | `settings/worker_local_multi_process.env` |
| Lint tests | `tests/test_k3s_setup_sh.py`, `tests/test_helm_chart.py` |
