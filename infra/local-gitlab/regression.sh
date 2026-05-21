#!/usr/bin/env bash
# ============================================================================
# infra/local-gitlab/regression.sh
#
# One-command regression for Option 1: SDLCMA stack on the WSL host (systemd
# units) + Windows docker-compose GitLab. Wraps setup.sh + smoke + teardown
# with the pieces that were always done by hand:
#   - Phase 1 env check (deps + GitLab reachable + Redis up)
#   - Phase 2 version detection — checks the running orchestrator's startup
#     log for `worker_completed_key=` (added late session); absence ⇒ stale
#   - Phase 3 update — restart systemd to pick up newer code
#   - Phase 4 setup (idempotent setup.sh + ENV swap + webhook URL coercion)
#   - Phase 5 smoke (trigger pipeline, poll for new MR)
#   - Phase 6 restore (teardown, ENV revert, webhook revert)
#
# Exit codes:  0 PASS / 2 FAIL / 3 TIMEOUT / 4 PRE-FLIGHT FAIL
#
# Usage:
#   bash infra/local-gitlab/regression.sh \
#     [--timeout 300] [--no-update] [--no-teardown] [--keep-env]
# ============================================================================
set -uo pipefail

START_TS=$(date +%s)
say()   { printf '[t+%4ds] %s\n' $(($(date +%s) - START_TS)) "$*"; }
abort() { echo; echo "ABORT: $1" >&2; exit "${2:-4}"; }

TIMEOUT=300
NO_UPDATE=0
NO_TEARDOWN=0
KEEP_ENV=0
while [ $# -gt 0 ]; do
  case "$1" in
    --timeout)     TIMEOUT="$2"; shift 2 ;;
    --no-update)   NO_UPDATE=1;   shift ;;
    --no-teardown) NO_TEARDOWN=1; shift ;;
    --keep-env)    KEEP_ENV=1;    shift ;;
    -h|--help)     sed -n '4,25p' "$0"; exit 0 ;;
    *) abort "unknown arg: $1" 4 ;;
  esac
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="$REPO_DIR/settings/.env"
PROJ_API="http://localhost:8080/api/v4"
PROJ_ID=2
HOOK_ID=1
WEBHOOK_URL="http://host.docker.internal:8000/webhook"
REQUIRED_ENV="local_multi_process"
SVC=(sdlcma-local-gateway sdlcma-local-orchestrator)

# ── Phase 1: environment check ──────────────────────────────────────────────
say "Phase 1: environment check"
for c in curl systemctl python3; do
  command -v "$c" >/dev/null || abort "missing dep: $c"
done
[ -f "$REPO_DIR/settings/worker_local_multi_process.env" ] \
  || abort "settings/worker_local_multi_process.env missing"
TOK=$(grep -oE 'GITLAB_PRIVATE_TOKEN=.+' \
   "$REPO_DIR/settings/worker_local_multi_process.env" | cut -d= -f2-)
[ -n "$TOK" ] || abort "GITLAB_PRIVATE_TOKEN missing in worker env"
curl -sf -m5 -o /dev/null "$PROJ_API/projects/$PROJ_ID" -H "PRIVATE-TOKEN: $TOK" \
  || abort "GitLab project $PROJ_ID unreachable at $PROJ_API"
say "  ✓ deps + GitLab reachable"

# ── Phase 2: version detection ──────────────────────────────────────────────
say "Phase 2: version detection"
NEEDS_RESTART=0
if [ "$(systemctl is-active sdlcma-local-orchestrator 2>/dev/null)" != "active" ]; then
  NEEDS_RESTART=1
  say "  orchestrator inactive — will start"
elif sudo -n journalctl -u sdlcma-local-orchestrator -n 40 --no-pager 2>/dev/null \
   | grep -q "worker_completed_key"; then
  say "  ✓ running orchestrator has new code (worker_completed_key found)"
else
  NEEDS_RESTART=1
  say "  running orchestrator is stale (no worker_completed_key in startup) — will restart"
fi

# ── ENV swap (record prior for restore) ─────────────────────────────────────
CURRENT_ENV=$(grep -E '^ENV=' "$ENV_FILE" | head -1 | cut -d= -f2)
PRIOR_ENV="$CURRENT_ENV"
if [ "$CURRENT_ENV" != "$REQUIRED_ENV" ]; then
  say "  ENV=$CURRENT_ENV → $REQUIRED_ENV (will restore unless --keep-env)"
  python3 - "$ENV_FILE" "$REQUIRED_ENV" <<'PYEOF'
import re, sys
p, want = sys.argv[1], sys.argv[2]
with open(p) as f: s = f.read()
s = re.sub(r"^(ENV=(?!" + want + r"$).+)$", r"#\1", s, flags=re.M)
s = re.sub(r"^#(ENV=" + want + r"$)", r"\1", s, flags=re.M)
if not re.search(r"^ENV=" + want + r"$", s, re.M):
    s += f"\nENV={want}\n"
with open(p, "w") as f: f.write(s)
PYEOF
  NEEDS_RESTART=1  # systemd reads .env at start; ENV change must restart
fi

# ── Webhook URL coercion ───────────────────────────────────────────────────
PRIOR_HOOK=$(curl -sf -m5 "$PROJ_API/projects/$PROJ_ID/hooks/$HOOK_ID" \
   -H "PRIVATE-TOKEN: $TOK" \
   | python3 -c "import json,sys;print(json.load(sys.stdin).get('url',''))" 2>/dev/null || echo "")
if [ -n "$PRIOR_HOOK" ] && [ "$PRIOR_HOOK" != "$WEBHOOK_URL" ]; then
  say "  webhook $PRIOR_HOOK → $WEBHOOK_URL (will restore)"
  curl -sf -m5 -X PUT "$PROJ_API/projects/$PROJ_ID/hooks/$HOOK_ID" \
    -H "PRIVATE-TOKEN: $TOK" -H "Content-Type: application/json" \
    -d "{\"url\":\"$WEBHOOK_URL\",\"pipeline_events\":true,\"push_events\":false}" \
    >/dev/null || abort "webhook PUT failed"
elif [ -z "$PRIOR_HOOK" ]; then
  say "  WARN: cannot read current webhook (token scope?); proceeding"
fi

# Always-on restore (trap survives every exit path)
restore() {
  local rc=$?
  set +e
  if [ "$NO_TEARDOWN" = 0 ]; then
    say "Phase 6: restore"
    bash "$REPO_DIR/infra/local-gitlab/teardown.sh" >/dev/null 2>&1 \
      && say "  ✓ stack down" || say "  WARN: teardown non-zero"
  else
    say "Phase 6: --no-teardown → stack left running"
  fi
  if [ "$KEEP_ENV" = 0 ] && [ "$PRIOR_ENV" != "$REQUIRED_ENV" ]; then
    python3 - "$ENV_FILE" "$PRIOR_ENV" "$REQUIRED_ENV" <<'PYEOF'
import re, sys
p, prior, want = sys.argv[1], sys.argv[2], sys.argv[3]
with open(p) as f: s = f.read()
s = re.sub(r"^ENV=" + want + r"$", "#ENV=" + want, s, flags=re.M)
s = re.sub(r"^#(ENV=" + prior + r"$)", r"\1", s, flags=re.M)
with open(p, "w") as f: f.write(s)
PYEOF
    say "  ✓ ENV restored → $PRIOR_ENV"
  fi
  if [ "$KEEP_ENV" = 0 ] && [ -n "$PRIOR_HOOK" ] && [ "$PRIOR_HOOK" != "$WEBHOOK_URL" ]; then
    curl -sf -m5 -X PUT "$PROJ_API/projects/$PROJ_ID/hooks/$HOOK_ID" \
      -H "PRIVATE-TOKEN: $TOK" -H "Content-Type: application/json" \
      -d "{\"url\":\"$PRIOR_HOOK\",\"pipeline_events\":true,\"push_events\":false}" \
      >/dev/null && say "  ✓ webhook restored → $PRIOR_HOOK"
  fi
  exit "$rc"
}
trap restore EXIT

# ── Phase 3: update if stale ────────────────────────────────────────────────
say "Phase 3: update if stale"
if [ "$NO_UPDATE" = 1 ]; then
  say "  --no-update: skip"
elif [ "$NEEDS_RESTART" = 1 ]; then
  sudo systemctl restart "${SVC[@]}" 2>&1 | sed 's/^/  /' || true
  for s in "${SVC[@]}"; do
    [ "$(systemctl is-active "$s")" = active ] \
      || abort "$s failed to start (check: sudo journalctl -u $s -n 50)"
  done
  say "  ✓ systemd units restarted, all active"
else
  say "  current code is live, no restart needed"
fi

# ── Phase 4: setup (ensure ready state) ────────────────────────────────────
say "Phase 4: setup"
bash "$REPO_DIR/infra/local-gitlab/setup.sh" >/dev/null 2>&1 \
  || abort "setup.sh failed (check sudo journalctl -u sdlcma-local-orchestrator)"
curl -sf -m5 -o /dev/null http://localhost:8000/healthz \
  || abort "gateway /healthz not 200"
say "  ✓ gateway healthy, services active"

# ── Phase 5: smoke ──────────────────────────────────────────────────────────
say "Phase 5: smoke (timeout ${TIMEOUT}s)"
BASELINE=$(curl -sf -m5 "$PROJ_API/projects/$PROJ_ID/merge_requests?per_page=1&order_by=created_at&sort=desc" \
   -H "PRIVATE-TOKEN: $TOK" \
   | python3 -c "import json,sys;mrs=json.load(sys.stdin);print(mrs[0]['iid'] if mrs else 0)")
say "  baseline MR iid = $BASELINE"
PIPE=$(curl -sf -m5 -X POST "$PROJ_API/projects/$PROJ_ID/pipeline?ref=main" \
   -H "PRIVATE-TOKEN: $TOK" \
   | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('id',''))")
[ -n "$PIPE" ] || abort "trigger pipeline failed" 2
say "  triggered pipeline $PIPE"

POLL_START=$(date +%s)
RESULT=TIMEOUT
while [ $(($(date +%s) - POLL_START)) -lt "$TIMEOUT" ]; do
  NEW_MR=$(curl -sf -m5 "$PROJ_API/projects/$PROJ_ID/merge_requests?state=opened&order_by=created_at&sort=desc&per_page=3" \
     -H "PRIVATE-TOKEN: $TOK" \
     | python3 -c "
import json, sys
mrs = json.load(sys.stdin)
new = [m for m in mrs if m['iid'] > $BASELINE and m['source_branch'].startswith('auto/bf/')]
print('!{} {}'.format(new[0]['iid'], new[0]['source_branch'])) if new else print('')
")
  if [ -n "$NEW_MR" ]; then
    RESULT=PASS
    say "  ✓ MR opened: $NEW_MR"
    break
  fi
  ELAPSED=$(($(date +%s) - POLL_START))
  say "  [...] no new MR yet (t+${ELAPSED}s)"
  sleep 15
done

echo
case "$RESULT" in
  PASS)    say "=== PASS ===" ; exit 0 ;;
  TIMEOUT) say "=== TIMEOUT (no new auto/bf MR within ${TIMEOUT}s) ==="; exit 3 ;;
esac
