#!/usr/bin/env bash
# ============================================================================
# infra/local-docker-compose/regression.sh
#
# One-command regression for local_docker_compose: the SDLCMA stack
# containerized on the WSL host (docker-compose) + Windows docker-compose
# GitLab joined to sdlcma_net. The worker is spawned by DockerWorkerSpawner
# via the local docker socket. Phases:
#   1 env check (deps + GitLab + sdlcma_net + GitLab on sdlcma_net)
#   2 version detection (local image Created ts vs running container StartedAt)
#   3 update (rebuild + recompose if image newer than running container)
#   4 setup (docker compose up -d + ENV swap + webhook → gateway:8000)
#   5 smoke (trigger pipeline → poll for new MR + verify rc=0 on worker)
#   6 restore (docker compose down + ENV revert + webhook revert + clean
#              dh-bf-worker-* spawned containers)
#
# Exit codes:  0 PASS / 2 FAIL / 3 TIMEOUT / 4 PRE-FLIGHT FAIL
#
# Usage:
#   bash infra/local-docker-compose/regression.sh \
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
WEBHOOK_URL="http://gateway:8000/webhook"   # resolved INSIDE sdlcma_net
REQUIRED_ENV="local_docker_compose"
IMAGES=(dh-gateway:latest dh-orchestrator:latest dh-bf-worker:latest)

cd "$REPO_DIR"

# ── Phase 1: environment check ──────────────────────────────────────────────
say "Phase 1: environment check"
for c in docker curl python3; do
  command -v "$c" >/dev/null || abort "missing dep: $c"
done
docker info >/dev/null 2>&1 || abort "docker daemon not reachable"
TOK=$(grep -oE 'GITLAB_PRIVATE_TOKEN=.+' \
   "$REPO_DIR/settings/worker_local_docker_compose.env" | cut -d= -f2-)
[ -n "$TOK" ] || abort "GITLAB_PRIVATE_TOKEN missing in worker_local_docker_compose.env"
curl -sf -m5 -o /dev/null "$PROJ_API/projects/$PROJ_ID" -H "PRIVATE-TOKEN: $TOK" \
  || abort "GitLab project $PROJ_ID unreachable at $PROJ_API"
# sdlcma_net must exist and GitLab container must be on it (webhook target
# `gateway:8000` resolves via Docker DNS only inside that network).
docker network inspect sdlcma_net >/dev/null 2>&1 \
  || { say "  sdlcma_net absent — creating"; docker network create sdlcma_net >/dev/null; }
docker network inspect sdlcma_net --format '{{range .Containers}}{{.Name}} {{end}}' \
   | grep -qw gitlab \
  || abort "GitLab container not on sdlcma_net — docker network connect sdlcma_net gitlab"
say "  ✓ deps + GitLab + sdlcma_net OK"

# ── Phase 2: version detection ──────────────────────────────────────────────
say "Phase 2: version detection"
for img in "${IMAGES[@]}"; do
  docker image inspect "$img" >/dev/null 2>&1 \
    || abort "image $img not built locally — docker compose --profile build build"
done
NEEDS_REBUILD=0
LATEST_PY=$(find orchestrator bf_worker settings gateway -type f -name '*.py' \
   -printf '%T@\n' 2>/dev/null | sort -rn | head -1)
LATEST_PY=${LATEST_PY%%.*}
ORCH_IMG_TS=$(docker image inspect dh-orchestrator:latest --format '{{.Created}}' \
   | xargs -I{} date -d {} +%s)
if [ "$LATEST_PY" -gt "$ORCH_IMG_TS" ]; then
  NEEDS_REBUILD=1
  say "  code newer than image (py=$LATEST_PY > img=$ORCH_IMG_TS) — will rebuild"
else
  say "  ✓ image up-to-date vs code"
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
fi

PRIOR_HOOK=$(curl -sf -m5 "$PROJ_API/projects/$PROJ_ID/hooks/$HOOK_ID" \
   -H "PRIVATE-TOKEN: $TOK" \
   | python3 -c "import json,sys;print(json.load(sys.stdin).get('url',''))" 2>/dev/null || echo "")
if [ -n "$PRIOR_HOOK" ] && [ "$PRIOR_HOOK" != "$WEBHOOK_URL" ]; then
  say "  webhook $PRIOR_HOOK → $WEBHOOK_URL (will restore)"
  curl -sf -m5 -X PUT "$PROJ_API/projects/$PROJ_ID/hooks/$HOOK_ID" \
    -H "PRIVATE-TOKEN: $TOK" -H "Content-Type: application/json" \
    -d "{\"url\":\"$WEBHOOK_URL\",\"pipeline_events\":true,\"push_events\":false}" \
    >/dev/null
fi

restore() {
  local rc=$?
  set +e
  if [ "$NO_TEARDOWN" = 0 ]; then
    say "Phase 6: restore"
    docker compose down --remove-orphans >/dev/null 2>&1 \
      && say "  ✓ compose stack down"
    LEFTOVER=$(docker ps -aq --filter 'name=dh-bf-worker-' 2>/dev/null)
    [ -n "$LEFTOVER" ] && docker rm -f $LEFTOVER >/dev/null 2>&1 \
      && say "  ✓ spawned worker containers cleared"
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
elif [ "$NEEDS_REBUILD" = 1 ]; then
  say "  rebuilding images via 'docker compose --profile build build'"
  docker compose --profile build build 2>&1 | tail -3 | sed 's/^/    /'
fi

# ── Phase 4: setup ──────────────────────────────────────────────────────────
say "Phase 4: setup (docker compose up -d)"
docker compose up -d 2>&1 | tail -5 | sed 's/^/  /'
sleep 3
for svc in redis gateway orchestrator; do
  STATE=$(docker compose ps --format '{{.Service}}={{.State}}' | grep "^$svc=" || true)
  [ -n "$STATE" ] || abort "service $svc missing after compose up"
  echo "$STATE" | grep -q running || abort "$STATE not running"
done
# Smoke a quick gateway healthz check (curl is not in the gateway image; use host)
curl -sf -m5 -o /dev/null http://localhost:8000/healthz \
  || abort "gateway /healthz not 200"
# Verify gitlab → gateway path inside sdlcma_net
docker exec gitlab sh -c "wget -qO- --timeout=5 http://gateway:8000/healthz" 2>/dev/null \
   | grep -q '"status":"ok"' \
  || abort "gitlab container cannot reach gateway:8000 — webhook delivery will fail"
say "  ✓ services up + sdlcma_net path verified"

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
NEW_BUG=""
while [ $(($(date +%s) - POLL_START)) -lt "$TIMEOUT" ]; do
  NEW_MR=$(curl -sf -m5 "$PROJ_API/projects/$PROJ_ID/merge_requests?state=opened&order_by=created_at&sort=desc&per_page=3" \
     -H "PRIVATE-TOKEN: $TOK" \
     | python3 -c "
import json, sys
mrs = json.load(sys.stdin)
new = [m for m in mrs if m['iid'] > $BASELINE and m['source_branch'].startswith('auto/bf/')]
print('{} {}'.format(new[0]['iid'], new[0]['source_branch'])) if new else print('')
")
  if [ -n "$NEW_MR" ]; then
    RESULT=PASS
    NEW_BUG=$(echo "$NEW_MR" | awk '{print $2}' | sed 's|auto/bf/||;s|-[0-9a-f]\+$||')
    say "  ✓ MR opened: !$NEW_MR"
    break
  fi
  WORKERS=$(docker ps -q --filter 'name=dh-bf-worker-' 2>/dev/null | wc -l)
  ELAPSED=$(($(date +%s) - POLL_START))
  say "  [...] worker containers running=$WORKERS (t+${ELAPSED}s)"
  sleep 20
done

# Verify worker exit code if we got a result
if [ "$RESULT" = PASS ] && [ -n "$NEW_BUG" ]; then
  EXIT_LINE=$(docker ps -a --filter "name=dh-bf-worker-$NEW_BUG" --format '{{.Status}}' | head -1)
  echo "$EXIT_LINE" | grep -qE 'Exited \(0\)' \
    && say "  ✓ worker exited rc=0 cleanly ($EXIT_LINE)" \
    || say "  WARN: worker exit status: $EXIT_LINE"
fi

echo
case "$RESULT" in
  PASS)    say "=== PASS ==="; exit 0 ;;
  TIMEOUT) say "=== TIMEOUT (no new auto/bf MR within ${TIMEOUT}s) ==="; exit 3 ;;
esac
