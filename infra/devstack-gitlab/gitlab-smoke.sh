#!/usr/bin/env bash
# ============================================================================
# devstack+GITLAB end-to-end smoke test. Watches the in-VM stack for one full
# GitLab-mode run and asserts the terminal state (journal outcome="fixed" +
# an MR opened from auto/bf/<bug_id>-<sha8> into main, never a commit to main).
#
# The trigger (a FAILING pipeline on a project whose main actually has the bug)
# is external. Two ways to drive it:
#   1. Manual (default): push a failing commit / re-run the pipeline in GitLab.
#   2. RETRY_PIPELINE=<id>: this script retries an existing pipeline via the
#      GitLab API (token read from settings). Only useful if that pipeline's
#      ref still reproduces the bug.
#
#   bash infra/devstack-gitlab/gitlab-smoke.sh
#   RETRY_PIPELINE=10 bash infra/devstack-gitlab/gitlab-smoke.sh
#
# Exit 0 = a NEW journal record with outcome="fixed" and an opened MR appeared
# within TIMEOUT; non-zero on timeout / wrong outcome / unhealthy preconditions.
# ============================================================================
set -uo pipefail

VM_FIP="${VM_FIP:-172.24.4.71}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/sdlcma_1b}"
SSH_USER="${SSH_USER:-ubuntu}"
PROJECT_PATH="${PROJECT_PATH:-lishu2016/order_be}"
TIMEOUT="${TIMEOUT:-600}"            # seconds to wait for the run to finish
RETRY_PIPELINE="${RETRY_PIPELINE:-}"
VM="${SSH_USER}@${VM_FIP}"
SSH="ssh -i ${SSH_KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=8"

echo "=== preflight: services healthy ==="
$SSH "$VM" 'bash -s' <<'P' || { echo "VM stack unhealthy — run setup.sh"; exit 1; }
set -e
for s in gitlab-fwd sdlcma-gateway sdlcma-orchestrator redis-server; do
  [ "$(systemctl is-active "$s")" = active ] || { echo "$s not active"; exit 1; }
done
curl -sf -m6 -o /dev/null http://localhost:8000/healthz || { echo "gateway unhealthy"; exit 1; }
curl -sf -m6 -o /dev/null "http://localhost:8080/api/v4/projects" \
  -H "PRIVATE-TOKEN: $(grep -oE 'GITLAB_PRIVATE_TOKEN=.*' ~/sdlcma/settings/worker_local_multi_process.env | cut -d= -f2)" \
  || { echo "GitLab unreachable from VM"; exit 1; }
echo "preflight OK"
P
[ $? -eq 0 ] || exit 1
systemctl is-active sdlcma-relay8000.service >/dev/null || { echo "host relay not active — run setup.sh"; exit 1; }

BASE_J=$($SSH "$VM" 'ls ~/sdlcma/evaluation/journal/ 2>/dev/null | wc -l')
echo "journal baseline=${BASE_J}"

if [ -n "${RETRY_PIPELINE}" ]; then
  echo "=== retry pipeline #${RETRY_PIPELINE} via GitLab API ==="
  $SSH "$VM" "TOK=\$(grep -oE 'GITLAB_PRIVATE_TOKEN=.*' ~/sdlcma/settings/worker_local_multi_process.env | cut -d= -f2); \
    PID=\$(python3 -c \"import urllib.parse;print(urllib.parse.quote('${PROJECT_PATH}',safe=''))\"); \
    curl -sS -m10 -o /dev/null -w 'retry HTTP %{http_code}\n' -X POST \
      -H \"PRIVATE-TOKEN: \$TOK\" \
      http://localhost:8080/api/v4/projects/\$PID/pipelines/${RETRY_PIPELINE}/retry"
else
  cat <<MANUAL
=== TRIGGER NEEDED (manual) ===
Push a failing commit to ${PROJECT_PATH}, or re-run its pipeline in the GitLab
UI. (Webhook must already point at http://host.docker.internal:8000/webhook.)
Watching for up to ${TIMEOUT}s ...
MANUAL
fi

echo "=== watch for outcome=fixed + opened MR (<= ${TIMEOUT}s) ==="
DEADLINE=$(( $(date +%s) + TIMEOUT ))
RESULT=""
while [ "$(date +%s)" -lt "${DEADLINE}" ]; do
  read -r J LATEST <<<"$($SSH "$VM" 'ls -1t ~/sdlcma/evaluation/journal/ 2>/dev/null | { mapfile -t a; echo "${#a[@]} ${a[0]:-}"; }')"
  if [ "${J:-0}" -gt "${BASE_J}" ] && [ -n "${LATEST:-}" ]; then
    REC=$($SSH "$VM" "python3 -c \"import json,sys;d=json.load(open('/home/${SSH_USER}/sdlcma/evaluation/journal/${LATEST}/record.json'));print(d.get('outcome'),d.get('review_status'),d.get('review_url'),d.get('review_branch'))\" 2>/dev/null")
    echo "  latest=${LATEST} -> ${REC}"
    set -- ${REC}
    if [ "${1:-}" = "fixed" ] && [ "${2:-}" = "opened" ]; then
      RESULT="PASS"; OUTCOME="${REC}"; break
    fi
    # a new record that is NOT fixed/opened = a real failure, stop early
    if [ -n "${1:-}" ] && [ "${1:-}" != "None" ]; then
      RESULT="FAIL"; OUTCOME="${REC}"; break
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
  echo "Inspect: ssh -i ${SSH_KEY} ${VM} 'sudo journalctl -u sdlcma-orchestrator -n 200 --no-pager | grep -v json_data'"
  exit 2
else
  echo "=== TIMEOUT after ${TIMEOUT}s: no new journal record (was the pipeline triggered? does main actually have the bug?) ==="
  exit 3
fi
