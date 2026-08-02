#!/usr/bin/env bash
# ============================================================================
# infra/k3s/regression.sh — one-command end-to-end regression on k3s.
#
#   1 setup      (delegates to setup.sh)
#   2 smoke      (delegates to gitlab-smoke.sh → infra/k8s/gitlab-smoke.sh)
#   3 crossnode  (optional, --crossnode: proves a worker runs on the remote
#                 node; needs the image imported there first)
#   4 teardown   (unless --no-teardown)
#
# Simpler than the kind sibling in one way worth noting: there is no webhook
# rewriting. The kind path has to capture/restore the project's hook URL on
# every run because cloudflared mints a NEW trycloudflare URL on each pod
# restart. Here the entry point is a fixed NodePort on a fixed tailnet IP, so
# the hook is configured once and stays correct — nothing to save or restore,
# and no window where a crash leaves someone else's hook pointing at us.
#
# Usage:
#   bash infra/k3s/regression.sh [--crossnode] [--no-teardown] [--timeout 900]
#
# Exit: 0 PASS / 2 FAIL / 3 TIMEOUT / 4 pre-flight / 10 needs a human
# ============================================================================
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_DIR"

TIMEOUT=900
NO_TEARDOWN=0
CROSSNODE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --timeout)     TIMEOUT="$2"; shift 2 ;;
    --no-teardown) NO_TEARDOWN=1; shift ;;
    --crossnode)   CROSSNODE=1;   shift ;;
    -h|--help)     sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 4 ;;
  esac
done

START_TS=$(date +%s)
say() { printf '[t+%4ds] %s\n' $(($(date +%s) - START_TS)) "$*"; }

# ── 1: setup ───────────────────────────────────────────────────────────────
say "Phase 1: setup"
bash infra/k3s/setup.sh
RC=$?
if [ $RC -ne 0 ]; then
  say "setup failed (rc=$RC)"
  exit $RC
fi

# ── 2: smoke ───────────────────────────────────────────────────────────────
say "Phase 2: smoke"
TIMEOUT="$TIMEOUT" bash infra/k3s/gitlab-smoke.sh
SMOKE_RC=$?
say "smoke rc=$SMOKE_RC"

# ── 3: cross-node (opt-in) ─────────────────────────────────────────────────
CROSS_RC=0
if [ "$CROSSNODE" = 1 ]; then
  say "Phase 3: cross-node check"
  bash infra/k3s/crossnode-check.sh
  CROSS_RC=$?
  say "crossnode rc=$CROSS_RC"
else
  say "Phase 3: cross-node check skipped (--crossnode to include)"
fi

# ── 4: teardown ────────────────────────────────────────────────────────────
if [ "$NO_TEARDOWN" = 0 ]; then
  say "Phase 4: teardown"
  bash infra/k3s/teardown.sh | sed 's/^/  /'
else
  say "Phase 4: teardown skipped (--no-teardown)"
fi

echo
if [ $SMOKE_RC -eq 0 ] && [ $CROSS_RC -eq 0 ]; then
  say "=== PASS ==="
  exit 0
fi
say "=== FAIL (smoke=$SMOKE_RC crossnode=$CROSS_RC) ==="
[ $SMOKE_RC -ne 0 ] && exit $SMOKE_RC
exit $CROSS_RC
