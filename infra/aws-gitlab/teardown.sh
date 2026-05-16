#!/usr/bin/env bash
# Stop / remove the SDLCMA stack provisioned by provision.sh on a generic host.
#
#   HOST=ubuntu@1.2.3.4 SSH_KEY=~/.ssh/id_xxx bash infra/aws-gitlab/teardown.sh
#   FULL=1 HOST=... bash infra/aws-gitlab/teardown.sh   # also remove unit files
#
# Never touches the repo, evaluation/journal, or redis data.
set -uo pipefail

HOST="${HOST:?set HOST=user@ip}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
FULL="${FULL:-0}"
SSH="ssh -i ${SSH_KEY} -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"

$SSH "$HOST" "FULL=${FULL} bash -s" <<'REMOTE' 2>/dev/null || { echo "host unreachable"; exit 1; }
pkill -f 'bf_worker/bf_worker.py' 2>/dev/null || true
sudo systemctl stop sdlcma-cf sdlcma-orchestrator sdlcma-gateway 2>/dev/null || true
if [ "${FULL}" = 1 ]; then
  sudo systemctl disable sdlcma-cf sdlcma-orchestrator sdlcma-gateway 2>/dev/null || true
  sudo rm -f /etc/systemd/system/sdlcma-cf.service \
             /etc/systemd/system/sdlcma-gateway.service \
             /etc/systemd/system/sdlcma-orchestrator.service
  sudo systemctl daemon-reload
fi
redis-cli -n 15 DEL gateway:stream >/dev/null 2>&1 || true
echo "stopped: cf=$(systemctl is-active sdlcma-cf) gw=$(systemctl is-active sdlcma-gateway) orch=$(systemctl is-active sdlcma-orchestrator)"
REMOTE
echo "=== done (repo/journal/redis-data intact) ==="
