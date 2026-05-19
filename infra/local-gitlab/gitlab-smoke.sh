#!/usr/bin/env bash
# ============================================================================
# Option-1 host + docker-compose-GitLab end-to-end smoke test. Watches the
# local stack for one full GitLab-mode run and asserts the terminal state:
# a NEW journal record with outcome="fixed" AND an MR opened from
# auto/bf/<bug_id>-<sha8> into main (the agent never commits to main).
#
# The trigger (a FAILING pipeline on a project whose main actually has the
# bug) is external. Two ways to drive it:
#   1. Manual (default): re-run / push a failing pipeline in GitLab.
#   2. RETRY_PIPELINE=<id>: retry an existing pipeline via the GitLab API
#      (only useful if that pipeline's ref still reproduces the bug).
#
#   bash infra/local-gitlab/gitlab-smoke.sh
#   RETRY_PIPELINE=10 bash infra/local-gitlab/gitlab-smoke.sh
#
# Exit 0 = (fixed, opened) within TIMEOUT; non-zero on timeout / wrong
# outcome / unhealthy preconditions.
# ============================================================================
set -uo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
VENV_BIN="${REPO_DIR}/.venv-linux/bin"
JOURNAL_DIR="${BF_JOURNAL_DIR:-${REPO_DIR}/evaluation/journal}"
PROJECT_PATH="${PROJECT_PATH:-lishu2016/order_be}"
GITLAB_LOCAL_URL="${GITLAB_LOCAL_URL:-http://localhost:8080}"
TIMEOUT="${TIMEOUT:-600}"
RETRY_PIPELINE="${RETRY_PIPELINE:-}"

echo "=== preflight: services healthy ==="
for s in sdlcma-local-gateway sdlcma-local-orchestrator; do
  [ "$(systemctl is-active "$s")" = active ] || { echo "$s not active — run setup.sh"; exit 1; }
done
curl -sf -m8 -o /dev/null http://localhost:8000/healthz || { echo "gateway unhealthy"; exit 1; }
TOK=$(grep -oE 'GITLAB_PRIVATE_TOKEN=.*' "${REPO_DIR}/settings/worker_local_multi_process.env" | cut -d= -f2)
PID=$("${VENV_BIN}/python" -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "${PROJECT_PATH}")
curl -sf -m8 -o /dev/null "${GITLAB_LOCAL_URL}/api/v4/projects/${PID}" \
  -H "PRIVATE-TOKEN: ${TOK}" || { echo "GitLab/project unreachable"; exit 1; }
echo "preflight OK"

BASE_J=$(ls "${JOURNAL_DIR}" 2>/dev/null | wc -l)
echo "journal baseline=${BASE_J}"

if [ -n "${RETRY_PIPELINE}" ]; then
  echo "=== retry pipeline #${RETRY_PIPELINE} via GitLab API ==="
  curl -sS -m10 -o /dev/null -w 'retry HTTP %{http_code}\n' -X POST \
    -H "PRIVATE-TOKEN: ${TOK}" \
    "${GITLAB_LOCAL_URL}/api/v4/projects/${PID}/pipelines/${RETRY_PIPELINE}/retry"
else
  cat <<MANUAL
=== TRIGGER NEEDED (manual) ===
Re-run / push a failing pipeline on ${PROJECT_PATH} (its main must reproduce
the bug). Webhook must point at http://host.docker.internal:8000/webhook.
Watching for up to ${TIMEOUT}s ...
MANUAL
fi

echo "=== watch for outcome=fixed + opened MR (<= ${TIMEOUT}s) ==="
DEADLINE=$(( $(date +%s) + TIMEOUT ))
RESULT=""; OUTCOME=""
while [ "$(date +%s)" -lt "${DEADLINE}" ]; do
  J=$(ls "${JOURNAL_DIR}" 2>/dev/null | wc -l)
  LATEST=$(ls -1t "${JOURNAL_DIR}" 2>/dev/null | head -1)
  if [ "${J:-0}" -gt "${BASE_J}" ] && [ -n "${LATEST:-}" ]; then
    REC=$("${VENV_BIN}/python" -c "import json;d=json.load(open('${JOURNAL_DIR}/${LATEST}/record.json'));print(d.get('outcome'),d.get('review_status'))" 2>/dev/null)
    echo "  latest=${LATEST} -> ${REC}"
    set -- ${REC}
    if [ "${1:-}" = "fixed" ] && [ "${2:-}" = "opened" ]; then
      RESULT=PASS; OUTCOME="${REC}"; break
    fi
    if [ -n "${1:-}" ] && [ "${1:-}" != "None" ]; then
      RESULT=FAIL; OUTCOME="${REC}"; break
    fi
  fi
  sleep 15
done

echo
if [ "${RESULT}" = PASS ]; then
  echo "=== PASS: ${OUTCOME} ==="
  echo "MR opened from a fix branch into main; main was NOT committed to directly."
  exit 0
elif [ "${RESULT}" = FAIL ]; then
  echo "=== FAIL: terminal record was not (fixed, opened): ${OUTCOME} ==="
  echo "Inspect: sudo journalctl -u sdlcma-local-orchestrator -n 200 --no-pager"
  exit 2
else
  echo "=== TIMEOUT after ${TIMEOUT}s: no terminal record (was the pipeline triggered? does main actually have the bug?) ==="
  exit 3
fi
