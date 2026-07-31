#!/usr/bin/env bash
# Snapshot / restore the LLM gateway's cache between experiment arms.
#
# Why an arm needs this: the gateway runs in `mode: cache` (replay on hit,
# RECORD on miss). So an arm that misses writes what it sampled into the live
# db, and the NEXT arm hits on it. The arms then see different cache states —
# at which point the control arm is no longer a control. Restoring the same
# snapshot before every replaying arm makes them symmetric.
#
# The live db is WAL-mode: `cp` of it ships a silent EMPTY cache (the entries
# are in <db>-wal). Export therefore goes through tools/llm_cache_transfer.py,
# which reads through the WAL into one self-contained file. That exported file
# IS a plain single-file db, so restoring it is an ordinary copy.
#
# usage:
#   llm_cache_arm_reset.sh export  <snapshot.db>   # live -> snapshot (gateway may stay up)
#   llm_cache_arm_reset.sh restore <snapshot.db>   # stop gateway, snapshot -> live, start gateway
#   llm_cache_arm_reset.sh info                    # entry count of the live db
set -u

ACTION="${1:?export|restore|info}"
SNAP="${2:-}"
REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
PY="${PY:-$REPO/.venv/bin/python}"
LOG_DIR="${LOG_DIR:-$HOME/.sdlcma/w2/logs}"
LLM_GATEWAY_CONFIG="${LLM_GATEWAY_CONFIG:-$HOME/.sdlcma/w2/gw_w2.yaml}"
LLM_ENV_FILE="${LLM_ENV_FILE:-$HOME/.sdlcma_llm.env}"   # backend credentials
LLM_GATEWAY_PORT="${LLM_GATEWAY_PORT:-9000}"

# The live db path is whatever the gateway config says — never a second copy of
# that fact in a second place.
CACHE_DB="${CACHE_DB:-$(sed -n 's/^[[:space:]]*db_path:[[:space:]]*//p' "$LLM_GATEWAY_CONFIG" | head -1)}"
CACHE_DB="${CACHE_DB/#\~/$HOME}"
[ -n "$CACHE_DB" ] || { echo "no db_path in $LLM_GATEWAY_CONFIG" >&2; exit 2; }
cd "$REPO"

case "$ACTION" in
  info)
    "$PY" -m tools.llm_cache_transfer info --db "$CACHE_DB"
    ;;
  export)
    [ -n "$SNAP" ] || { echo "usage: $0 export <snapshot.db>" >&2; exit 2; }
    rm -f "$SNAP"
    # Verify, don't announce: an export that failed but printed "written" let a
    # whole control arm run without cache isolation before anyone noticed.
    "$PY" -m tools.llm_cache_transfer export --db "$CACHE_DB" --out "$SNAP" || {
      echo "export FAILED" >&2; exit 1; }
    [ -s "$SNAP" ] || { echo "export produced no file at $SNAP" >&2; exit 1; }
    echo "snapshot written: $SNAP ($(du -h "$SNAP" | cut -f1))"
    ;;
  restore)
    [ -f "$SNAP" ] || { echo "no such snapshot: $SNAP" >&2; exit 2; }
    # The gateway needs its backend credentials to start at all — it validates
    # them at startup and exits. Restarting it from a bare environment leaves
    # every worker of the next arm talking to a dead port.
    # shellcheck disable=SC1090
    [ -f "$LLM_ENV_FILE" ] && . "$LLM_ENV_FILE"
    pkill -f 'llm_gateway\.app' 2>/dev/null
    sleep 2
    rm -f "$CACHE_DB" "$CACHE_DB-wal" "$CACHE_DB-shm"
    cp "$SNAP" "$CACHE_DB"          # the EXPORT is a plain single-file db
    chmod 644 "$CACHE_DB"
    LLM_GATEWAY_CONFIG="$LLM_GATEWAY_CONFIG" setsid nohup "$PY" -m uvicorn \
        llm_gateway.app:app --host 127.0.0.1 --port "$LLM_GATEWAY_PORT" \
        >> "$LOG_DIR/llmgw.log" 2>&1 < /dev/null &
    # Wait for it, and make failure FATAL: a restore that leaves the gateway
    # down turns the whole arm into errors, which looks like a product failure.
    for _ in $(seq 1 20); do
      sleep 2
      code=$(curl -s -o /dev/null -w "%{http_code}" \
             "http://127.0.0.1:$LLM_GATEWAY_PORT/v1/models" || true)
      [ "$code" = "200" ] && break
    done
    [ "$code" = "200" ] || {
      echo "llm_gateway did NOT come back on :$LLM_GATEWAY_PORT (last=$code)" >&2
      tail -5 "$LOG_DIR/llmgw.log" >&2
      exit 1; }
    echo "llm_gateway restarted on :$LLM_GATEWAY_PORT -> 200"
    "$PY" -m tools.llm_cache_transfer info --db "$CACHE_DB" | grep -i entries
    ;;
  *)
    echo "unknown action: $ACTION" >&2; exit 2;;
esac
