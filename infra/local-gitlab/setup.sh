#!/usr/bin/env bash
# ============================================================================
# SDLCMA GitLab-mode test harness — "Option 1": run gateway+orchestrator+worker
# DIRECTLY on the WSL host, with GitLab as a docker-compose container on the
# Windows host. Idempotent: safe to re-run. Counterpart: teardown.sh.
#
# This is the no-VM sibling of infra/devstack-gitlab/ (Option 2). Because the
# stack runs on the host as the developer user, everything the DevStack VM
# needed for parity drops away:
#   - no DevStack/OpenStack/br-ex/NAT/secgroup            (no VM)
#   - no rsync to a VM                                    (run in place)
#   - no socat localhost:8080 -> GitLab                   (Docker Desktop's
#       WSL2 localhostForwarding already makes the Windows-published GitLab
#       :8080 reachable from WSL as localhost:8080, so local_multi_process's
#       hardcoded gitlab.local->localhost:8080 rewrite needs ZERO code change)
#   - no host webhook relay                               (the gateway IS the
#       :8000 listener; GitLab container -> host.docker.internal:8000 ->
#       Windows -> WSL localhostForwarding -> this gateway)
#   - no `python-is-python3` / git `--system` gotchas     (units run as the
#       dev user with HOME + the venv on PATH, so `python -m venv` resolves
#       and the user's --global git identity applies — see devstack memory
#       gotchas #2/#3, which only bit the root-owned VM units)
#
# It DOES codify the one non-obvious host change: the devstack Option-2
# `sdlcma-relay8000.service` (socat WSL:8000 -> dead VM 172.24.4.71:8000)
# squats on :8000 and would misroute every webhook away from the local
# gateway. setup.sh stops+disables it; teardown.sh can restore it.
#
# Purpose: rehearse the public-IP-host path. local_multi_process is chosen
# deliberately — HTTP-token auth (http://user:token@localhost:8080), no SSH —
# which is exactly the model `gitlab_saas` mirrors over HTTPS for the AWS /
# gitlab.com track. (local_docker_compose rewrites to the `gitlab` container
# hostname + SSH: only valid inside the compose network, opposite direction.)
#
# Run from the WSL host (repo root or anywhere — paths are derived):
#   bash infra/local-gitlab/setup.sh
#
# Preconditions the script assumes (one-time, not created here):
#   - GitLab docker-compose up on Windows, published :8080->80 / :2222->22,
#     external_url http://gitlab.local.
#   - A Redis reachable at localhost:6379 (the user's redis container is fine).
#   - .venv-linux present in the repo with deps installed.
#   - settings/.env = local_multi_process and the matching worker/orchestrator
#     env files filled in (token, LLM key).
# ============================================================================
set -euo pipefail

# ---- Derived (portable) ----------------------------------------------------
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
RUN_USER="${RUN_USER:-$(id -un)}"
RUN_HOME="${RUN_HOME:-$(getent passwd "${RUN_USER}" | cut -d: -f6)}"
VENV_BIN="${REPO_DIR}/.venv-linux/bin"
REDIS_URL="${REDIS_URL:-redis://localhost:6379/15}"
GITLAB_LOCAL_URL="${GITLAB_LOCAL_URL:-http://localhost:8080}"

say() { printf '\n=== %s ===\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ -x "${VENV_BIN}/python" ]  || die "missing ${VENV_BIN}/python — create .venv-linux and install deps first"
[ -x "${VENV_BIN}/uvicorn" ] || die "missing ${VENV_BIN}/uvicorn — install deps into .venv-linux"

# ---- Phase 1: settings sanity ---------------------------------------------
say "1. settings/.env must select local_multi_process"
ENV_NAME=$(grep -E '^ENV=' "${REPO_DIR}/settings/.env" | head -1 | cut -d= -f2)
[ "${ENV_NAME}" = "local_multi_process" ] \
  || die "settings/.env ENV=${ENV_NAME:-<unset>} (expected local_multi_process); edit it and re-run"
for f in worker_local_multi_process.env orchestrator_local_multi_process.env; do
  [ -f "${REPO_DIR}/settings/${f}" ] || die "missing settings/${f}"
done
grep -q '^GITLAB_PRIVATE_TOKEN=.\+' "${REPO_DIR}/settings/worker_local_multi_process.env" \
  || die "GITLAB_PRIVATE_TOKEN not filled in settings/worker_local_multi_process.env"
echo "  ENV=local_multi_process, env files present, token set"

# ---- Phase 2: free :8000 (stop the devstack Option-2 relay) ----------------
say "2. Yield :8000 from the devstack Option-2 relay (if present)"
if systemctl cat sdlcma-relay8000.service >/dev/null 2>&1; then
  sudo systemctl disable --now sdlcma-relay8000.service 2>/dev/null || true
  echo "  sdlcma-relay8000.service stopped+disabled (teardown.sh can restore it)"
else
  echo "  no sdlcma-relay8000.service — nothing to yield"
fi
# Anything else still on :8000 is a hard stop (don't fight an unknown listener).
if ss -tlnH 'sport = :8000' 2>/dev/null | grep -q LISTEN; then
  OWNER=$(sudo ss -tlnpH 'sport = :8000' 2>/dev/null | grep -o 'users:(("[^"]*"' | head -1)
  die ":8000 still held by ${OWNER:-an unknown process} — free it before re-running"
fi

# ---- Phase 3: dependency reachability (design checks, not readiness pings) --
say "3. Redis + GitLab reachable"
"${VENV_BIN}/python" - "${REDIS_URL}" <<'PY' || die "Redis unreachable at ${REDIS_URL}"
import sys, redis
redis.from_url(sys.argv[1], socket_connect_timeout=5).ping()
print("  redis OK", sys.argv[1])
PY
code=$(curl -sS -m8 -o /dev/null -w '%{http_code}' "${GITLAB_LOCAL_URL}/" || echo 000)
case "${code}" in
  2*|3*) echo "  GitLab OK ${GITLAB_LOCAL_URL}/ -> HTTP ${code}" ;;
  *) die "GitLab not reachable at ${GITLAB_LOCAL_URL}/ (HTTP ${code}); is the Windows docker-compose GitLab up?" ;;
esac

# ---- Phase 4: systemd units (host, run as the dev user) --------------------
# User=${RUN_USER} + HOME + venv on PATH => no python-is-python3 / git --system
# gotchas (those only hit the root-owned VM units in Option 2).
say "4. Install + start sdlcma-local-{gateway,orchestrator} (systemd, host)"
UNIT_PATH="${VENV_BIN}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

sudo tee /etc/systemd/system/sdlcma-local-gateway.service >/dev/null <<UNIT
[Unit]
Description=SDLCMA gateway (Option 1: host + docker-compose GitLab)
After=network.target

[Service]
User=${RUN_USER}
Environment=HOME=${RUN_HOME}
Environment=PATH=${UNIT_PATH}
WorkingDirectory=${REPO_DIR}
ExecStart=${VENV_BIN}/uvicorn gateway.gateway:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/systemd/system/sdlcma-local-orchestrator.service >/dev/null <<UNIT
[Unit]
Description=SDLCMA orchestrator (Option 1: host + docker-compose GitLab)
After=network.target sdlcma-local-gateway.service

[Service]
User=${RUN_USER}
Environment=HOME=${RUN_HOME}
Environment=PATH=${UNIT_PATH}
WorkingDirectory=${REPO_DIR}
ExecStart=${VENV_BIN}/python -m orchestrator.orchestrator
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now sdlcma-local-gateway.service sdlcma-local-orchestrator.service
sleep 4
for s in sdlcma-local-gateway sdlcma-local-orchestrator; do
  printf '  %-28s %s\n' "$s" "$(systemctl is-active "$s")"
done

# ---- Phase 5: verify the webhook landing path ------------------------------
say "5. Gateway health + project webhook"
curl -sS -m8 -o /dev/null -w "  gateway /healthz HTTP %{http_code}\n" \
  http://localhost:8000/healthz || true
TOK=$(grep -oE 'GITLAB_PRIVATE_TOKEN=.*' "${REPO_DIR}/settings/worker_local_multi_process.env" | cut -d= -f2)
PROJECT_PATH="${PROJECT_PATH:-lishu2016/order_be}"
PID=$("${VENV_BIN}/python" -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "${PROJECT_PATH}")
curl -sS -m8 -H "PRIVATE-TOKEN: ${TOK}" "${GITLAB_LOCAL_URL}/api/v4/projects/${PID}/hooks" \
  | "${VENV_BIN}/python" -c 'import sys,json;[print("  hook:",h["url"],"pipeline=",h.get("pipeline_events"),"job=",h.get("job_events")) for h in json.load(sys.stdin)]' \
  2>/dev/null || echo "  (could not read project hooks — set one manually, see below)"

cat <<DONE

=== SETUP COMPLETE (Option 1) ===
Stack: gateway+orchestrator on the WSL host (systemd), agent env
local_multi_process, GitLab = docker-compose on Windows (localhost:8080).

If the project webhook above is NOT
  URL:     http://host.docker.internal:8000/webhook
  Trigger: Pipeline events  (Job events optional)
set it in GitLab UI: ${PROJECT_PATH} -> Settings -> Webhooks.

Trigger a FAILING pipeline on ${PROJECT_PATH} (its main must reproduce a bug),
then watch:
  sudo journalctl -u sdlcma-local-orchestrator -f
or assert end-to-end:
  bash infra/local-gitlab/gitlab-smoke.sh
Expected terminal state: journal record outcome="fixed", an MR opened from
auto/bf/<bug_id>-<sha8> into main (the agent never commits to main).

Teardown: bash infra/local-gitlab/teardown.sh
  RESTORE_RELAY=1 bash infra/local-gitlab/teardown.sh   # also re-enable the
                                                          devstack Option-2 relay
DONE
