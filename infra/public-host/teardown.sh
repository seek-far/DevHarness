#!/usr/bin/env bash
# ============================================================================
# SDLCMA cloud rehearsal — take the public-host stack DOWN.
#
# Reboot-OFF semantic: stop+remove the containers AND remove the swapfile, so
# after a host reboot NOTHING SDLCMA comes back — only infra/public-host/
# setup.sh brings it up again. Reversible; does not touch Docker itself,
# sales-retro, or Caddy.
#
#   bash infra/public-host/teardown.sh
#   FULL=1 bash infra/public-host/teardown.sh   # also rmi the dh-* images +
#                                                  remove sdlcma_net
#
# Run from the WSL dev box. Idempotent.
# ============================================================================
set -uo pipefail

HOST="${HOST:-82.165.48.174}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/sales_deploy}"
SSH_USER="${SSH_USER:-root}"
REMOTE_DIR="${REMOTE_DIR:-/root/sdlcma-stack}"
FULL="${FULL:-0}"
SSH="ssh -i ${SSH_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=12"
T="${SSH_USER}@${HOST}"

echo "=== stop + remove the stack ==="
$SSH "$T" "cd ${REMOTE_DIR} 2>/dev/null && docker compose -f docker-compose.yml -f docker-compose.public-host.yml down --remove-orphans" 2>/dev/null \
  || echo "(stack already down / ${REMOTE_DIR} absent)"
# kill any lingering per-bug worker containers (spawned via the Docker socket,
# not part of compose)
$SSH "$T" 'ids=$(docker ps -aq --filter "name=dh-bf-worker-"); [ -n "$ids" ] && docker rm -f $ids >/dev/null 2>&1 || true; echo "  worker containers cleared"'

echo "=== remove swapfile (so reboot does not re-enable it) ==="
$SSH "$T" 'if swapon --show=NAME --noheadings | grep -q /swapfile; then swapoff /swapfile && rm -f /swapfile && echo "  swapfile off+removed"; else echo "  no /swapfile"; fi'

if [ "${FULL}" = 1 ]; then
  echo "=== FULL: rmi dh-* images + rm sdlcma_net ==="
  $SSH "$T" 'docker rmi -f dh-gateway:latest dh-orchestrator:latest dh-bf-worker:latest >/dev/null 2>&1 || true; docker network rm sdlcma_net >/dev/null 2>&1 || true; echo "  images+net removed"'
fi

echo "=== done. Reboot will NOT start SDLCMA. Re-up only via setup.sh ==="
echo "    (Docker daemon, sales-retro, Caddy left untouched.)"
echo "    Restore the co-tenant app: ssh root@${HOST} then sales_02/deploy/setup.sh"
