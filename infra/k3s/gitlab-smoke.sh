#!/usr/bin/env bash
# ============================================================================
# infra/k3s/gitlab-smoke.sh — end-to-end smoke on k3s, against the self-hosted
# GitLab that lives on the same host as the cluster.
#
# A thin wrapper, on purpose. The acceptance logic (trigger a failing
# pipeline, poll for a new auto/bf MR, report bf-worker Job counts while
# waiting) is identical to the kind path, and a forked copy of an acceptance
# criterion drifts into a DIFFERENT criterion without anyone noticing. So
# this only sets the three things that differ — GitLab endpoint, project, and
# which kubeconfig `kubectl` should use — and execs the shared script.
#
# Env (defaults shown):
#   GITLAB_API=http://ls4900/api/v4
#   PROJECT_PATH=root/k3s-smoke      # main-branch CI fails on purpose;
#                                    # exactly ONE job (the orchestrator's
#                                    # parser reads builds[0].id)
#   ENV_FILE=settings/worker_local_multi_process.env
#   K3S_KUBECONFIG=~/.kube/k3s.yaml
#   TIMEOUT=600
# ============================================================================
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

export GITLAB_API="${GITLAB_API:-http://ls4900/api/v4}"
export PROJECT_PATH="${PROJECT_PATH:-root/k3s-smoke}"
export ENV_FILE="${ENV_FILE:-settings/worker_local_multi_process.env}"
export NAMESPACE="${NAMESPACE:-sdlcma}"
export TIMEOUT="${TIMEOUT:-600}"

# Dedicated kubeconfig — never a global `kubectl config use-context`, which
# is how this host lost its kind context in the first place.
export KUBECONFIG="${K3S_KUBECONFIG:-$HOME/.kube/k3s.yaml}"
[ -r "$KUBECONFIG" ] || { echo "ABORT: no $KUBECONFIG — run infra/k3s/setup.sh first" >&2; exit 4; }

exec bash infra/k8s/gitlab-smoke.sh "$@"
