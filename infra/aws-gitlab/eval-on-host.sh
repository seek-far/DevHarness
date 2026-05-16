#!/usr/bin/env bash
# devstack/AWS-agnostic EVALUATION test on a generic host (Phase 0.5 box or
# EC2). Self-contained: no GitLab, no webhook. Runs the sweep on the host,
# pulls results back, prints the report. Exit 0 = report produced.
#
#   HOST=ubuntu@1.2.3.4 SSH_KEY=~/.ssh/id_xxx bash infra/aws-gitlab/eval-on-host.sh
#   CONFIG=configs/memory_vs_baseline.json HOST=... bash .../eval-on-host.sh
set -euo pipefail

HOST="${HOST:?set HOST=user@ip}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
REPO_DIR="${REPO_DIR:-/mnt/d/my_git/sdlcma/v08}"
CONFIG="${CONFIG:-configs/baseline.json}"
SSH="ssh -i ${SSH_KEY} -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"

$SSH "$HOST" 'test -x ~/sdlcma/.venv-linux/bin/python' \
  || { echo "host not provisioned — run provision.sh first"; exit 1; }

echo "=== run sweep on host (config=${CONFIG}) ==="
RUN_ID=$($SSH "$HOST" "cd ~/sdlcma && . .venv-linux/bin/activate && \
  python -m evaluation.cli run --config '${CONFIG}' >/tmp/eval.log 2>&1; \
  ls -1t evaluation/runs/ | head -1")
[ -n "${RUN_ID}" ] || { echo "no run_id produced"; $SSH "$HOST" 'tail -30 /tmp/eval.log'; exit 1; }

echo "=== pull results -> host repo ==="
mkdir -p "${REPO_DIR}/evaluation/runs/${RUN_ID}"
rsync -az -e "$SSH" "${HOST}:~/sdlcma/evaluation/runs/${RUN_ID}/" \
  "${REPO_DIR}/evaluation/runs/${RUN_ID}/"

echo "=== report ==="
$SSH "$HOST" "cd ~/sdlcma && . .venv-linux/bin/activate && \
  python -m evaluation.cli report '${RUN_ID}'"
echo "=== PASS: sweep ${RUN_ID} complete ==="
