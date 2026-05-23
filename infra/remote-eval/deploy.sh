#!/usr/bin/env bash
# ============================================================================
# SDLCMA remote-eval harness — push source to a Tailscale-reachable host,
# set up a Python venv, point the worker at the host's local vLLM
# (127.0.0.1:8000), and prepare the worker env file. Idempotent: first run
# does a full sync + setup; later runs are pure incremental rsync (uv sync
# only re-resolves when requirements changed).
#
# Intended for evaluation-mode runs against the bundled fixtures. No
# GitLab / Redis / orchestrator are deployed — `bench run` exercises the
# agent in-process. The remote uses an existing ENV (local_multi_process);
# the discriminator "this is a self-hosted backend" lives entirely in the
# three LLM fields of worker_<ENV>.env, not in a new env name.
#
# Run from the WSL dev box:  bash infra/remote-eval/deploy.sh
#
# After the script:
#   ssh -i ~/.ssh/ls4090 ls@100.81.178.68
#   cd ~/sdlcma && source .venv-linux/bin/activate
#   uv run python -m evaluation.cli run --config configs/baseline.json
# ============================================================================
set -euo pipefail

HOST="${HOST:-100.81.178.68}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/ls4090}"
SSH_USER="${SSH_USER:-ls}"
REMOTE_DIR="${REMOTE_DIR:-/home/ls/sdlcma}"
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

VLLM_URL="${VLLM_URL:-http://127.0.0.1:8000/v1}"
WORKER_ENV_FILE="settings/worker_local_multi_process.env"

SSH_OPTS="-i ${SSH_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=12"
SSH="ssh ${SSH_OPTS}"
RSYNC_E="ssh ${SSH_OPTS}"
T="${SSH_USER}@${HOST}"

say() { printf '\n=== %s ===\n' "$*"; }

say "0. preflight"
test -r "${SSH_KEY}" || { echo "SSH_KEY not readable: ${SSH_KEY}" >&2; exit 1; }
test -d "${REPO_DIR}/bf_worker" || { echo "REPO_DIR doesn't look like the SDLCMA repo: ${REPO_DIR}" >&2; exit 1; }

say "1. ssh reachable"
$SSH "$T" 'echo ok && uname -a && (command -v python3 || true)'

say "2. rsync source → ${T}:${REMOTE_DIR}"
$SSH "$T" "mkdir -p ${REMOTE_DIR}"
# --delete keeps the remote tree as a faithful mirror of local (drops files
# you've removed locally). The exclude list keeps secrets (real *.env),
# build artefacts, and per-run output OFF the remote.
rsync -az --delete --info=stats1 \
  -e "${RSYNC_E}" \
  --exclude '.git/' \
  --exclude '.venv-linux/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.pytest_cache/' \
  --exclude '.mypy_cache/' \
  --exclude '.ruff_cache/' \
  --exclude 'evaluation/runs/' \
  --exclude 'evaluation/journal/' \
  --exclude '*.tfvars' \
  --exclude '*.tfstate*' \
  --exclude '.terraform/' \
  --exclude '.idea/' \
  --exclude '.vscode/' \
  --exclude '*.swp' \
  --include 'settings/.env' \
  --exclude 'settings/*.env' \
  "${REPO_DIR}/" "${T}:${REMOTE_DIR}/"

say "3. install uv (if missing) + create venv + uv pip install -r requirements.txt"
$SSH "$T" "REMOTE_DIR=${REMOTE_DIR} bash -s" <<'REMOTE'
set -euo pipefail
cd "${REMOTE_DIR}"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "  installing uv ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
fi
uv --version
if [ ! -d .venv-linux ]; then
  echo "  creating .venv-linux ..."
  uv venv .venv-linux
fi
# shellcheck disable=SC1091
. .venv-linux/bin/activate
uv pip install -r requirements.txt
REMOTE

say "4. discover vLLM model id from remote ${VLLM_URL}/models"
MODEL=$($SSH "$T" "curl -sf '${VLLM_URL}/models' | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d[\"data\"][0][\"id\"])'") || {
  echo "FAILED to query ${VLLM_URL}/models on remote — is vLLM up?" >&2
  exit 1
}
echo "  model = ${MODEL}"

say "5. write settings/.env + ${WORKER_ENV_FILE} on remote"
# settings/.env pins the ENV name the two-step loader reads. The worker env
# file is generated from the tracked .example, patching only the three LLM
# lines and a placeholder GITLAB_USERNAME (WorkerSettings requires the
# field but evaluation mode doesn't actually talk to GitLab).
$SSH "$T" "REMOTE_DIR=${REMOTE_DIR} MODEL='${MODEL}' bash -s" <<'REMOTE'
set -euo pipefail
cd "${REMOTE_DIR}"
cat > settings/.env <<EOF
ENV=local_multi_process
EOF
cp -f settings/worker_local_multi_process.env.example settings/worker_local_multi_process.env
python3 - "${MODEL}" <<'PY'
import sys, re, pathlib
model = sys.argv[1]
p = pathlib.Path("settings/worker_local_multi_process.env")
src = p.read_text()
def setline(text, key, value):
    pat = re.compile(rf'^{re.escape(key)}=.*$', re.M)
    line = f'{key}={value}'
    return pat.sub(line, text) if pat.search(text) else text.rstrip() + f'\n{line}\n'
src = setline(src, "LLM_API_BASE_URL", "http://127.0.0.1:8000/v1")
src = setline(src, "LLM_MODEL", model)
# Drop LLM_API_KEY entirely: WorkerSettings defaults it to "EMPTY" for
# self-hosted backends. Leaving the .example placeholder ("your_llm_api_key")
# in place would override the default with a string the backend may reject.
src = re.sub(r'^LLM_API_KEY=.*\n?', '', src, flags=re.M)
src = setline(src, "GITLAB_USERNAME", "eval-only-not-used")
p.write_text(src)
PY
echo "--- ${PWD}/${0##*=} settings/worker_local_multi_process.env ---"
sed 's/^/  /' settings/worker_local_multi_process.env
REMOTE

say "DONE."
cat <<EOF
Next steps (run interactively or wrap in a separate smoke script):

  ssh -i ${SSH_KEY} ${T}
  cd ${REMOTE_DIR}
  source .venv-linux/bin/activate
  uv run python -m evaluation.cli run --config configs/baseline.json

Pull results back:
  rsync -az -e "${RSYNC_E}" \\
    ${T}:${REMOTE_DIR}/evaluation/runs/ ${REPO_DIR}/evaluation/runs/
EOF
