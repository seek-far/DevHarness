#!/usr/bin/env bash
# ============================================================================
# devstack EVALUATION test: run the evaluation sweep INSIDE the DevStack VM,
# pull results back to the host, print the comparison report.
#
# Self-contained and deterministic — no GitLab, no external trigger, no
# webhook. Just exercises evaluation mode on the VM. Assumes setup.sh has
# provisioned the VM (repo + .venv-linux + settings present).
#
#   bash infra/devstack-gitlab/eval-in-vm.sh                       # baseline
#   CONFIG=configs/memory_vs_baseline.json bash .../eval-in-vm.sh  # other spec
#
# Exit 0 = sweep ran and a report was produced; non-zero otherwise.
# ============================================================================
set -euo pipefail

VM_FIP="${VM_FIP:-172.24.4.71}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/sdlcma_1b}"
SSH_USER="${SSH_USER:-ubuntu}"
REPO_DIR="${REPO_DIR:-/mnt/d/my_git/sdlcma/v08}"
CONFIG="${CONFIG:-configs/baseline.json}"
VM="${SSH_USER}@${VM_FIP}"
SSH="ssh -i ${SSH_KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=8"

echo "=== preflight: VM venv present ==="
$SSH "$VM" 'test -x ~/sdlcma/.venv-linux/bin/python' \
  || { echo "VM not provisioned — run setup.sh first"; exit 1; }

echo "=== run sweep on VM (config=${CONFIG}) ==="
RUN_ID=$($SSH "$VM" "cd ~/sdlcma && . .venv-linux/bin/activate && \
  python -m evaluation.cli run --config '${CONFIG}' 2>&1 | tee /tmp/eval.log >&2; \
  ls -1t evaluation/runs/ | head -1")
[ -n "${RUN_ID}" ] || { echo "no run_id produced"; exit 1; }
echo "run_id=${RUN_ID}"

echo "=== pull results -> host ==="
mkdir -p "${REPO_DIR}/evaluation/runs/${RUN_ID}"
rsync -az -e "$SSH" \
  "${VM}:~/sdlcma/evaluation/runs/${RUN_ID}/" \
  "${REPO_DIR}/evaluation/runs/${RUN_ID}/"

echo "=== report ==="
$SSH "$VM" "cd ~/sdlcma && . .venv-linux/bin/activate && \
  python -m evaluation.cli report '${RUN_ID}'"

echo "=== PASS: sweep ${RUN_ID} complete, results in evaluation/runs/${RUN_ID}/ ==="
