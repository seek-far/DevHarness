#!/usr/bin/env bash
# ============================================================================
# SDLCMA cloud rehearsal — bring the containerized gitlab.com stack UP on a
# public-IP host (the "Phase 0.5" box). Counterpart: teardown.sh.
#
# Reboot-OFF semantic (project-wide): the compose services carry NO restart
# policy and teardown.sh runs `compose down` + `swapoff`, so a host reboot
# does NOT resurrect anything — ONLY this setup.sh brings the stack up.
#
# Strategy on a 2 GB box: transfer the locally-built images (docker save|load)
# instead of building on the host (a host `pip install langchain…` would risk
# OOM). Adds a 4 GB swapfile as the documented mandatory mitigation.
#
# Run from the WSL dev box (where the dh-* images were built by the Stage-2
# flow):  bash infra/public-host/setup.sh
#
# PRE: stop the co-tenant app first to free RAM —
#      ssh root@<host> 'bash /path/to/sales_02/deploy/teardown.sh'
#      (sales-retro ~hundreds of MB; this rehearsal needs the headroom).
#      You must have already set the gitlab.com project webhook to
#      http://<HOST>:8000/webhook  (Pipeline events, SSL verification OFF).
# ============================================================================
set -euo pipefail

HOST="${HOST:-82.165.48.174}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/sales_deploy}"
SSH_USER="${SSH_USER:-root}"
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SWAP_GB="${SWAP_GB:-4}"
REMOTE_DIR="${REMOTE_DIR:-/root/sdlcma-stack}"
IMAGES="dh-gateway:latest dh-orchestrator:latest dh-bf-worker:latest"
SSH="ssh -i ${SSH_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=12"
T="${SSH_USER}@${HOST}"

say() { printf '\n=== %s ===\n' "$*"; }

say "0. local images present?"
for img in $IMAGES; do
  docker image inspect "$img" >/dev/null 2>&1 || {
    echo "missing $img — build first: docker compose -f docker-compose.yml -f docker-compose.public-host.yml --profile build build" >&2
    exit 1; }
done
echo "  $IMAGES"

say "1. ssh reachable"
$SSH "$T" 'echo ok && . /etc/os-release && echo "$PRETTY_NAME"'

say "2. host: install Docker if absent + ${SWAP_GB}G swap (idempotent)"
$SSH "$T" "SWAP_GB=${SWAP_GB} bash -s" <<'REMOTE'
set -euo pipefail
if ! command -v docker >/dev/null 2>&1; then
  echo "  installing Docker Engine ..."
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
docker --version
# 4G swap — NOT added to /etc/fstab on purpose: a reboot must not bring the
# rehearsal environment back; setup.sh is the only path up. teardown removes it.
if ! swapon --show=NAME --noheadings | grep -q '/swapfile'; then
  echo "  creating /swapfile (${SWAP_GB}G) ..."
  fallocate -l "${SWAP_GB}G" /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=$((SWAP_GB*1024)) status=none
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
fi
free -m | awk '/Swap:/{print "  swap MB total="$2}'
docker network inspect sdlcma_net >/dev/null 2>&1 || docker network create sdlcma_net >/dev/null
echo "  sdlcma_net ready"
REMOTE

say "3. ship locally-built images (docker save | ssh docker load)"
if [ "${FORCE_SHIP:-0}" != 1 ] && \
   $SSH "$T" "for i in $IMAGES; do docker image inspect \$i >/dev/null 2>&1 || exit 1; done"; then
  echo "  all images already on host — skip (FORCE_SHIP=1 to re-ship)"
else
  docker save $IMAGES | $SSH "$T" 'docker load'
fi

say "4. ship compose files"
$SSH "$T" "mkdir -p ${REMOTE_DIR}"
scp -i "${SSH_KEY}" -o IdentitiesOnly=yes \
  "${REPO_DIR}/docker-compose.yml" "${REPO_DIR}/docker-compose.public-host.yml" \
  "${T}:${REMOTE_DIR}/" >/dev/null
echo "  -> ${REMOTE_DIR}/"

say "5. host: bring the stack up (no restart policy → reboot-OFF)"
$SSH "$T" "cd ${REMOTE_DIR} && docker compose -f docker-compose.yml -f docker-compose.public-host.yml up -d"
sleep 4
$SSH "$T" "cd ${REMOTE_DIR} && docker compose -f docker-compose.yml -f docker-compose.public-host.yml ps --format '{{.Service}} {{.State}}'"

say "6. health"
$SSH "$T" 'curl -fsS -m8 -o /dev/null -w "  host gateway /healthz HTTP %{http_code}\n" http://127.0.0.1:8000/healthz || echo "  gateway not healthy yet"'
$SSH "$T" "cd ${REMOTE_DIR} && docker compose -f docker-compose.yml -f docker-compose.public-host.yml logs orchestrator 2>&1 | grep -m1 -iE 'starting env=|worker_spawner' || true"

say "7. cloudflared public URL (inbound webhook ingress)"
CF_URL=""
for i in $(seq 1 20); do
  CF_URL=$($SSH "$T" "cd ${REMOTE_DIR} && docker compose -f docker-compose.yml -f docker-compose.public-host.yml logs cloudflared 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1" 2>/dev/null)
  [ -n "$CF_URL" ] && break
  sleep 3
done
echo "  ${CF_URL:-NOT-READY (check: docker compose ... logs cloudflared)}"

cat <<DONE

=== SETUP COMPLETE (public host ${HOST}) ===
Inbound :8000 is blocked by the IONOS provider firewall, so the webhook goes
through the cloudflared tunnel (ephemeral — changes every cloudflared start).
Set this on the gitlab.com project webhook (Pipeline events):
  ${CF_URL:-<run: docker compose ... logs cloudflared>}/webhook
Trigger a failing pipeline on the gitlab.com test project, then verify on
gitlab.com:  merge_requests?source_branch=auto/bf/<bug>-<sha8>  +  that
fix-branch pipeline = success.
Co-tenant: sales-retro should be DOWN (sales_02/deploy/teardown.sh) for the
duration — 2 GB box.
Teardown (reboot-OFF): bash infra/public-host/teardown.sh
DONE
