#!/usr/bin/env bash
# End-to-end GitLab-mode smoke against gitlab.com, on a generic host
# (Phase 0.5 box or EC2). Watches one full run and asserts the terminal
# state: journal outcome="fixed" + an MR opened from auto/bf/<bug_id>-<sha8>
# into main (the agent never commits to main).
#
# Trigger is external (a FAILING pipeline on a gitlab.com project whose main
# actually has the bug). Either push a failing commit / re-run the pipeline
# manually, or pass RETRY_PIPELINE=<id> to retry one via the GitLab API.
#
#   HOST=ubuntu@1.2.3.4 SSH_KEY=~/.ssh/id_xxx \
#   PROJECT_PATH=mygroup/buggy-proj bash infra/aws-gitlab/gitlab-smoke.sh
#   RETRY_PIPELINE=42 HOST=... PROJECT_PATH=... bash .../gitlab-smoke.sh
#
# Exit 0 = (fixed, opened) within TIMEOUT; 2 = other terminal record; 3 = timeout.
set -uo pipefail

HOST="${HOST:?set HOST=user@ip}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
PROJECT_PATH="${PROJECT_PATH:?set PROJECT_PATH=group/project on gitlab.com}"
GITLAB_API="${GITLAB_API:-https://gitlab.com/api/v4}"
TIMEOUT="${TIMEOUT:-600}"
RETRY_PIPELINE="${RETRY_PIPELINE:-}"
SSH="ssh -i ${SSH_KEY} -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"

echo "=== preflight ==="
$SSH "$HOST" 'bash -s' <<'P' || { echo "host stack unhealthy — run provision.sh"; exit 1; }
set -e
for s in redis-server sdlcma-gateway sdlcma-orchestrator sdlcma-cf; do
  [ "$(systemctl is-active "$s")" = active ] || { echo "$s not active"; exit 1; }
done
curl -sf -m6 -o /dev/null http://localhost:8000/healthz || { echo "gateway down"; exit 1; }
grep -q 'your_gitlab_com_access_token' ~/sdlcma/settings/worker_gitlab_saas.env \
  && { echo "token placeholder not filled"; exit 1; } || true
URL=$(sudo journalctl -u sdlcma-cf --no-pager 2>/dev/null | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1)
echo "webhook URL = ${URL}/webhook"
P
[ $? -eq 0 ] || exit 1

BASE_J=$($SSH "$HOST" 'ls ~/sdlcma/evaluation/journal/ 2>/dev/null | wc -l')
echo "journal baseline=${BASE_J}"

if [ -n "${RETRY_PIPELINE}" ]; then
  echo "=== retry pipeline #${RETRY_PIPELINE} via gitlab.com API ==="
  $SSH "$HOST" "TOK=\$(grep -oE 'GITLAB_PRIVATE_TOKEN=.*' ~/sdlcma/settings/worker_gitlab_saas.env | cut -d= -f2); \
    PID=\$(python3 -c \"import urllib.parse;print(urllib.parse.quote('${PROJECT_PATH}',safe=''))\"); \
    curl -sS -m10 -o /dev/null -w 'retry HTTP %{http_code}\n' -X POST \
      -H \"PRIVATE-TOKEN: \$TOK\" '${GITLAB_API}'/projects/\$PID/pipelines/${RETRY_PIPELINE}/retry"
else
  echo "=== TRIGGER NEEDED: push a failing commit / re-run the pipeline on ${PROJECT_PATH} ==="
fi

echo "=== watch (<= ${TIMEOUT}s) ==="
DEADLINE=$(( $(date +%s) + TIMEOUT )); RESULT=""
while [ "$(date +%s)" -lt "${DEADLINE}" ]; do
  read -r J LATEST <<<"$($SSH "$HOST" 'cd ~/sdlcma/evaluation/journal 2>/dev/null && { n=$(ls|wc -l); l=$(ls -1t|head -1); echo "$n $l"; }')"
  if [ "${J:-0}" -gt "${BASE_J}" ] && [ -n "${LATEST:-}" ]; then
    REC=$($SSH "$HOST" "cd ~/sdlcma/evaluation/journal/${LATEST} && python3 -c 'import json;d=json.load(open(\"record.json\"));print(d.get(\"outcome\"),d.get(\"review_status\"),d.get(\"review_url\"))'" 2>/dev/null)
    echo "  latest=${LATEST} -> ${REC}"
    set -- ${REC}
    if [ "${1:-}" = fixed ] && [ "${2:-}" = opened ]; then RESULT=PASS; OUT="${REC}"; break; fi
    if [ -n "${1:-}" ] && [ "${1:-}" != None ]; then RESULT=FAIL; OUT="${REC}"; break; fi
  fi
  sleep 15
done

echo
case "${RESULT}" in
  PASS) echo "=== PASS: ${OUT} (MR opened from fix branch into main; main untouched) ==="; exit 0;;
  FAIL) echo "=== FAIL: terminal record not (fixed,opened): ${OUT} ==="; exit 2;;
  *)    echo "=== TIMEOUT ${TIMEOUT}s: no qualifying record (pipeline triggered? main has the bug?) ==="; exit 3;;
esac
