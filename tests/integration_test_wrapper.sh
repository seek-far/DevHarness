#!/usr/bin/env bash
# ============================================================================
# tests/integration_test_wrapper.sh
#
# One-command regression for integration_test.py. Pre-flights ENV (must be
# local_multi_process — the test uses subprocess spawner; under
# local_docker_compose the spawned worker container can't reach the host's
# 127.0.0.1:6379), pre-flights Redis, runs the test, and restores ENV.
#
# Exit codes:
#   0  PASS
#   2  FAIL (integration_test exited non-zero / returned ok=false)
#   3  TIMEOUT (max wall-clock for the underlying test)
#   4  PRE-FLIGHT FAIL (deps / Redis / mutually-exclusive stack)
#
# Usage:
#   bash tests/integration_test_wrapper.sh [--timeout 600] [--keep-env]
# ============================================================================
set -uo pipefail

START_TS=$(date +%s)
say()   { printf '[t+%4ds] %s\n' $(($(date +%s) - START_TS)) "$*"; }
abort() { echo; echo "ABORT: $1" >&2; exit "${2:-4}"; }

TIMEOUT=600
KEEP_ENV=0
while [ $# -gt 0 ]; do
  case "$1" in
    --timeout)  TIMEOUT="$2"; shift 2 ;;
    --keep-env) KEEP_ENV=1;   shift ;;
    -h|--help)
      sed -n '4,18p' "$0"; exit 0 ;;
    *) abort "unknown arg: $1" 4 ;;
  esac
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_DIR/settings/.env"
REQUIRED_ENV="local_multi_process"

# ── Phase 1: environment check ──────────────────────────────────────────────
say "Phase 1: environment check"
command -v python3   >/dev/null || abort "missing dep: python3"
command -v uv        >/dev/null || abort "missing dep: uv"
[ -f "$REPO_DIR/.venv-linux/bin/python" ] \
  || abort "missing .venv-linux; run: uv venv .venv-linux && uv sync"
[ -f "$REPO_DIR/integration_test.py" ] \
  || abort "integration_test.py not found under $REPO_DIR"
say "  ✓ deps + venv"

# Redis must be reachable at the default URL (the test isolates db=15).
python3 - <<'PYEOF' || abort "Redis at redis://127.0.0.1:6379/15 unreachable"
import sys, socket
try:
    s = socket.create_connection(("127.0.0.1", 6379), timeout=3); s.close()
except Exception as e:
    print(f"redis probe failed: {e}", file=sys.stderr); sys.exit(1)
PYEOF
say "  ✓ Redis 127.0.0.1:6379 reachable"

# Mutually-exclusive with the Option-1 systemd stack (shared db15 +
# gateway:stream + group orchestrator-group-mp; project memory:
# project_dual_orchestrator_stream_contention).
if systemctl is-active sdlcma-local-orchestrator >/dev/null 2>&1; then
  abort "Option-1 systemd stack is active and shares db15/gateway:stream — \
teardown first: bash infra/local-gitlab/teardown.sh"
fi
say "  ✓ no conflicting stack"

# ── Phase 2: ENV detection + swap ───────────────────────────────────────────
say "Phase 2: ENV detection"
CURRENT_ENV=$(grep -E '^ENV=' "$ENV_FILE" | head -1 | cut -d= -f2)
PRIOR_ENV="$CURRENT_ENV"
if [ "$CURRENT_ENV" = "$REQUIRED_ENV" ]; then
  say "  ✓ ENV=$REQUIRED_ENV already"
else
  say "  ENV=$CURRENT_ENV → $REQUIRED_ENV (will restore on exit)"
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
fi

restore_env() {
  [ "$KEEP_ENV" = 1 ] && return
  [ "$PRIOR_ENV" = "$CURRENT_ENV" ] && return
  python3 - "$ENV_FILE" "$PRIOR_ENV" "$REQUIRED_ENV" <<'PYEOF'
import re, sys
p, prior, want = sys.argv[1], sys.argv[2], sys.argv[3]
with open(p) as f: s = f.read()
s = re.sub(r"^ENV=" + want + r"$", "#ENV=" + want, s, flags=re.M)
s = re.sub(r"^#(ENV=" + prior + r"$)", r"\1", s, flags=re.M)
with open(p, "w") as f: f.write(s)
PYEOF
  say "  ✓ ENV restored to $PRIOR_ENV"
}
trap restore_env EXIT

# ── Phase 3: no update needed (integration_test is in-process) ──────────────
say "Phase 3: version detection — N/A (integration_test runs in-process)"

# ── Phase 4: no setup needed ────────────────────────────────────────────────
say "Phase 4: setup — N/A"

# ── Phase 5: run the test ───────────────────────────────────────────────────
say "Phase 5: running integration_test.py (timeout ${TIMEOUT}s)"
LOG="$(mktemp -t sdlcma-integration-XXXXXX.log)"
set +e
# Project rule (memory: feedback_integration_test_command): always activate
# .venv-linux + use `uv run python`.
( cd "$REPO_DIR" && source .venv-linux/bin/activate \
  && timeout "$TIMEOUT" uv run python integration_test.py ) >"$LOG" 2>&1 &
TEST_PID=$!
# stream a one-line progress beat while the subprocess runs
while kill -0 $TEST_PID 2>/dev/null; do
  sleep 30
  ELAPSED=$(($(date +%s) - START_TS))
  STEP=$(grep -oE '\[Step [A-Z]\][^|]*' "$LOG" | tail -1 | head -c 80)
  say "  [...] still running ${ELAPSED}s — last step: ${STEP:-<none>}"
done
wait $TEST_PID; RC=$?
set -e

if [ "$RC" -eq 124 ]; then
  say "  ✗ timed out after ${TIMEOUT}s"
  RESULT=TIMEOUT
elif [ "$RC" -ne 0 ]; then
  say "  ✗ exited rc=$RC"
  RESULT=FAIL
else
  # The test prints a JSON verdict at the end with ok=true / ok=false.
  VERDICT_OK=$(grep -oE '"ok":\s*(true|false)' "$LOG" | tail -1 | grep -oE 'true|false')
  if [ "$VERDICT_OK" = "true" ]; then
    say "  ✓ ok=true"
    RESULT=PASS
  else
    say "  ✗ ok=$VERDICT_OK (or no JSON verdict)"
    RESULT=FAIL
  fi
fi

# ── Phase 6: restore (handled by trap) ─────────────────────────────────────
say "Phase 6: restore — ENV swap reverted via trap"

echo
case "$RESULT" in
  PASS)    say "=== PASS ==="; exit 0 ;;
  FAIL)    say "=== FAIL  (log: $LOG, tail follows) ==="; tail -20 "$LOG"; exit 2 ;;
  TIMEOUT) say "=== TIMEOUT (log: $LOG) ==="; exit 3 ;;
esac
