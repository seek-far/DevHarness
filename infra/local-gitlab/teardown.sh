#!/usr/bin/env bash
# ============================================================================
# Teardown / cleanup for the Option-1 host harness (setup.sh).
#
#   bash infra/local-gitlab/teardown.sh                   # stop+disable units
#   FULL=1          bash infra/local-gitlab/teardown.sh   # also remove unit files
#   RESTORE_RELAY=1 bash infra/local-gitlab/teardown.sh   # re-enable the
#                                                           devstack Option-2
#                                                           sdlcma-relay8000
#
# Never touches: the repo, evaluation/journal, Redis, the Windows GitLab
# container, or the GitLab project webhook.
# ============================================================================
set -uo pipefail

FULL="${FULL:-0}"
RESTORE_RELAY="${RESTORE_RELAY:-0}"

echo "=== stop host stack + kill stale workers ==="
pkill -f 'bf_worker/bf_worker.py' 2>/dev/null || true
sudo systemctl stop sdlcma-local-orchestrator sdlcma-local-gateway 2>/dev/null || true
if [ "${FULL}" = 1 ]; then
  sudo systemctl disable sdlcma-local-orchestrator sdlcma-local-gateway 2>/dev/null || true
  sudo rm -f /etc/systemd/system/sdlcma-local-gateway.service \
             /etc/systemd/system/sdlcma-local-orchestrator.service
  sudo systemctl daemon-reload
fi
printf '  %-28s %s\n' sdlcma-local-gateway      "$(systemctl is-active sdlcma-local-gateway)"
printf '  %-28s %s\n' sdlcma-local-orchestrator "$(systemctl is-active sdlcma-local-orchestrator)"

# multi_process uses Redis db15; drop just the gateway stream so a fresh run
# starts clean (leave the rest of the DB and the journal intact).
"$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/.venv-linux/bin/python" - <<'PY' 2>/dev/null || true
import redis
redis.from_url("redis://localhost:6379/15", socket_connect_timeout=3).delete("gateway:stream")
print("  cleared gateway:stream (db15)")
PY

if [ "${RESTORE_RELAY}" = 1 ]; then
  if systemctl list-unit-files 2>/dev/null | grep -q '^sdlcma-relay8000.service'; then
    echo "=== re-enable devstack Option-2 relay (sdlcma-relay8000) ==="
    sudo systemctl enable --now sdlcma-relay8000.service 2>/dev/null || true
    printf '  %-28s %s\n' sdlcma-relay8000 "$(systemctl is-active sdlcma-relay8000)"
  else
    echo "  (no sdlcma-relay8000.service unit to restore)"
  fi
fi

echo "=== done. (repo, journal, Redis, GitLab, webhook left intact) ==="
