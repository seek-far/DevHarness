#!/usr/bin/env bash
# Bring up the ver99 stack (redis -> llm_gateway -> gateway -> orchestrator),
# optionally with the W2 resume switches on. Idempotent: each service is stopped
# before it is started.
#
# Why this is a script and not a checklist: the two orderings below are silent
# failures, both actually hit on ls4900.
#   * REDIS_URL must be exported BEFORE the webhook gateway starts — there is no
#     settings/gateway_*.env, so GatewaySettings otherwise defaults to db0 while
#     the orchestrator reads db15 and every webhook vanishes.
#   * MINI_IMPL / BF_STEP_CHECKPOINT must be exported BEFORE the orchestrator
#     starts — the subprocess spawner hands workers `os.environ.copy()`, so
#     exporting them afterwards silently disables resume.
#
# usage:
#   RESUME=1 infra/swebench-gitlab/ver99_stack_up.sh            # W2 resume on
#   infra/swebench-gitlab/ver99_stack_up.sh                     # plain ver99
#
# Host-specific inputs come from the environment (or the two credential files a
# prepared host already has):
#   LLM_ENV_FILE   default ~/.sdlcma_llm.env      (backend API key)
#   GITLAB_ENV_FILE default ~/.sdlcma_gitlab.env  (GITLAB_API / token)
#   LLM_GATEWAY_CONFIG  gateway yaml (cache db path lives in here)
set -u

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
PY="${PY:-$REPO/.venv/bin/python}"
LOG_DIR="${LOG_DIR:-$HOME/.sdlcma/w2/logs}"       # deliberately NOT /tmp: a reboot clears it
LLM_ENV_FILE="${LLM_ENV_FILE:-$HOME/.sdlcma_llm.env}"
GITLAB_ENV_FILE="${GITLAB_ENV_FILE:-$HOME/.sdlcma_gitlab.env}"
LLM_GATEWAY_CONFIG="${LLM_GATEWAY_CONFIG:-$HOME/.sdlcma/w2/gw_w2.yaml}"
REDIS_CONTAINER="${REDIS_CONTAINER:-m1-redis}"
RESUME="${RESUME:-0}"

mkdir -p "$LOG_DIR"
cd "$REPO"
# shellcheck disable=SC1090
[ -f "$LLM_ENV_FILE" ] && . "$LLM_ENV_FILE"
# shellcheck disable=SC1090
[ -f "$GITLAB_ENV_FILE" ] && . "$GITLAB_ENV_FILE"

export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export LITELLM_LOCAL_MODEL_COST_MAP=True
export REDIS_URL="${REDIS_URL:-redis://localhost:6379/15}"   # BEFORE the gateway

docker start "$REDIS_CONTAINER" >/dev/null 2>&1
sleep 1

pkill -f 'llm_gateway\.app' 2>/dev/null
pkill -f 'uvicorn gateway\.gateway' 2>/dev/null
pkill -f 'orchestrator\.orchestrator' 2>/dev/null
sleep 2

LLM_GATEWAY_CONFIG="$LLM_GATEWAY_CONFIG" setsid nohup "$PY" -m uvicorn \
    llm_gateway.app:app --host 127.0.0.1 --port "${LLM_GATEWAY_PORT:-9000}" \
    > "$LOG_DIR/llmgw.log" 2>&1 < /dev/null &
sleep 6

setsid nohup "$PY" -m uvicorn gateway.gateway:app \
    --host 0.0.0.0 --port "${GATEWAY_PORT:-8000}" \
    > "$LOG_DIR/gw.log" 2>&1 < /dev/null &
sleep 3

export BF_AGENT_CONFIG="${BF_AGENT_CONFIG:-configs/swebench/gitlab_ver99.json}"
export LLM_API_BASE_URL="${LLM_API_BASE_URL:-http://127.0.0.1:${LLM_GATEWAY_PORT:-9000}/v1}"
export BF_CI_WAIT_TIMEOUT="${BF_CI_WAIT_TIMEOUT:-1200}"
if [ "$RESUME" = "1" ]; then
  export MINI_IMPL=vendored          # BF_STEP_CHECKPOINT=file raises without it
  export BF_STEP_CHECKPOINT=file
fi
setsid nohup "$PY" -m orchestrator.orchestrator \
    > "$LOG_DIR/orch.log" 2>&1 < /dev/null &
sleep 5

echo "=== processes ==="
ps -eo pid,args | grep -E "[u]vicorn (gateway|llm_gateway)|[o]rchestrator\.orchestrator"
echo "=== health ==="
curl -s -o /dev/null -w "llm_gateway /v1/models: %{http_code}\n" \
     "http://127.0.0.1:${LLM_GATEWAY_PORT:-9000}/v1/models"
curl -s -o /dev/null -w "gateway /docs: %{http_code}\n" \
     "http://127.0.0.1:${GATEWAY_PORT:-8000}/docs"
grep -i "cache enabled" "$LOG_DIR/llmgw.log" | tail -1
echo "=== orchestrator environment (resume switches must show when RESUME=1) ==="
tr '\0' '\n' < "/proc/$(pgrep -f '[o]rchestrator.orchestrator' | head -1)/environ" \
  | grep -E '^(MINI_IMPL|BF_STEP_CHECKPOINT|BF_AGENT_CONFIG|LLM_API_BASE_URL|REDIS_URL)='
