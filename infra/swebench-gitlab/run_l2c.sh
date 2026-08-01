#!/usr/bin/env bash
# L2c — concurrent chaos acceptance for the W2 intra-loop step checkpoint.
#
# Runs one arm per name in --arms, then diffs the chaos arm against the control
# arm.  Default A,A2,C is the shape the design asks for:
#
#   A   control      resume ON, nothing killed        -> the per-instance baseline
#   A2  control'     identical to A                   -> run-to-run noise floor
#   C   chaos        resume ON, ~1/3 of workers -9'd  -> the thing under test
#
# A2 exists because of W1's hard lesson: the same code run twice does not
# produce the same resolved set, so a chaos-vs-control difference is only
# readable against a control-vs-control difference.  Skip it and you will
# misattribute noise to the feature.
#
# Prerequisites (see docs/swebench.md + tests/TESTING.md §1c):
#   * the ver99 stack is up with MINI_IMPL=vendored and BF_STEP_CHECKPOINT=file
#     exported BEFORE the orchestrator started (the spawner passes its own
#     environment to workers; exporting afterwards is a no-op),
#   * every instance in --instances has an `instance/<id>` branch and is
#     covered by the LLM cache being replayed.
#
# usage:
#   infra/swebench-gitlab/run_l2c.sh                      # A,A2,C then verify
#   ARMS=C infra/swebench-gitlab/run_l2c.sh               # chaos arm only
#   CONCURRENCY=8 KILL_RATE=0.5 infra/swebench-gitlab/run_l2c.sh
set -u

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
W2_DIR="${W2_DIR:-$HOME/.sdlcma/w2}"
PY="${PY:-$REPO/.venv/bin/python}"
INSTANCES_FILE="${INSTANCES_FILE:-$W2_DIR/instances.txt}"
ARMS="${ARMS:-A,A2,C}"
CONCURRENCY="${CONCURRENCY:-15}"
KILL_RATE="${KILL_RATE:-0.34}"
MIN_STEP="${MIN_STEP:-2}"
MAX_STEP="${MAX_STEP:-6}"
POLL_TIMEOUT="${POLL_TIMEOUT:-2400}"
CONTROL_ARM="${CONTROL_ARM:-A}"
CHAOS_ARM="${CHAOS_ARM:-C}"
# L3 knobs (all off by default ⇒ the L2c behaviour above is unchanged):
#   CACHE_ISOLATION=1  first arm records misses, every later arm starts from a
#                      snapshot of the cache as it stood after that first arm.
#                      Without it the second arm hits on what the first one
#                      sampled and the arms are no longer comparable.
#   ONLY_INSTANCES=f   restrict chaos to the judgeable subset (see cache_hitrate.py)
# W2.5 knobs:
#   KILL_WINDOWS=…     which windows chaos may fire in, comma-separated:
#                      in_command,at_boundary,post_loop. Drawn evenly, because a
#                      step-count trigger alone almost never lands post-loop —
#                      and that window is where the finished-run memo lives.
#   SECOND_KILL_RATE=r fraction of already-resumed runs to kill AGAIN. The only
#                      way to exercise "the resume itself was interrupted".
#   STEP_LEDGER=marker run the chaos arm with W2's at-least-once semantics, as
#                      the A/B arm; verify_l2c then drops the two exactly-once
#                      assertions instead of failing them by design.
KILL_WINDOWS="${KILL_WINDOWS:-in_command,at_boundary,post_loop}"
SECOND_KILL_RATE="${SECOND_KILL_RATE:-0.0}"
STEP_LEDGER="${STEP_LEDGER:-ledger}"
CACHE_ISOLATION="${CACHE_ISOLATION:-0}"
CACHE_SNAPSHOT="${CACHE_SNAPSHOT:-$W2_DIR/arm_cache_snapshot.db}"
ONLY_INSTANCES="${ONLY_INSTANCES:-}"
GITLAB_ENV_FILE="${GITLAB_ENV_FILE:-$HOME/.sdlcma_gitlab.env}"

# sweep.py triggers baseline pipelines over the API — without credentials it
# exits before a single instance runs, which costs a whole arm.
if [ -z "${GITLAB_PRIVATE_TOKEN:-}" ] && [ -f "$GITLAB_ENV_FILE" ]; then
  # shellcheck disable=SC1090
  . "$GITLAB_ENV_FILE"
fi
: "${GITLAB_API:?set GITLAB_API/GITLAB_PRIVATE_TOKEN or provide $GITLAB_ENV_FILE}"

mkdir -p "$W2_DIR/arms" "$W2_DIR/logs"
[ -f "$INSTANCES_FILE" ] || { echo "no instance list at $INSTANCES_FILE" >&2; exit 2; }
INSTANCES=$(tr '\n' ' ' < "$INSTANCES_FILE")
cd "$REPO"

# The sweep driver loads the SWE-bench dataset; keep it offline/local.
export HF_DATASETS_OFFLINE="${HF_DATASETS_OFFLINE:-1}"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export LITELLM_LOCAL_MODEL_COST_MAP=True

run_arm() {
  local arm="$1" mode="$2"
  echo "=== arm $arm ($mode) — $(date '+%F %T') ==="
  rm -f "$W2_DIR/chaos.stop" "$W2_DIR/events_$arm.jsonl"

  local kill_flag="--no-kill"
  [ "$mode" = "chaos" ] && kill_flag=""
  local only_flag=""
  [ -n "$ONLY_INSTANCES" ] && only_flag="--only-instances $ONLY_INSTANCES"
  # shellcheck disable=SC2086
  setsid nohup "$PY" "$REPO/infra/swebench-gitlab/chaos_kill.py" $kill_flag $only_flag \
      --events "$W2_DIR/events_$arm.jsonl" --stop-file "$W2_DIR/chaos.stop" \
      --rate "$KILL_RATE" --min-step "$MIN_STEP" --max-step "$MAX_STEP" \
      --windows "$KILL_WINDOWS" --second-kill-rate "$SECOND_KILL_RATE" \
      > "$W2_DIR/logs/chaos_$arm.log" 2>&1 < /dev/null &
  sleep 2

  local start
  start=$("$PY" -c 'import time;print(time.time())')
  # shellcheck disable=SC2086
  "$PY" -u "$REPO/infra/swebench-gitlab/sweep.py" \
      --instances $INSTANCES --skip-setup --force \
      --concurrency "$CONCURRENCY" --poll-timeout "$POLL_TIMEOUT" \
      > "$W2_DIR/logs/sweep_$arm.log" 2>&1
  local rc=$?

  touch "$W2_DIR/chaos.stop"
  sleep 4
  local end
  end=$("$PY" -c 'import time;print(time.time())')
  printf '{"arm":"%s","mode":"%s","start":%s,"end":%s,"rc":%s}\n' \
      "$arm" "$mode" "$start" "$end" "$rc" > "$W2_DIR/arms/$arm.json"
  echo "arm=$arm mode=$mode rc=$rc"
  tail -3 "$W2_DIR/logs/sweep_$arm.log"
}

RESET="$REPO/infra/swebench-gitlab/llm_cache_arm_reset.sh"

IFS=',' read -ra ARM_LIST <<< "$ARMS"
# The recording arm pays for every miss and defines the cache state the other
# arms replay. It defaults to the first arm of THIS invocation, so a later
# invocation running only the chaos arm must be told which arm recorded
# (SNAPSHOT_ARM=A) — otherwise it would re-export instead of restoring, and the
# chaos arm would silently start from a different cache than the control.
SNAPSHOT_ARM="${SNAPSHOT_ARM:-${ARM_LIST[0]}}"

for arm in "${ARM_LIST[@]}"; do
  if [ "$CACHE_ISOLATION" = "1" ] && [ "$arm" != "$SNAPSHOT_ARM" ]; then
    echo "--- restoring the cache snapshot so arm $arm replays the same state ---"
    # Hard stop: an arm that quietly runs without isolation is not a control
    # arm, and nothing downstream can tell the difference afterwards.
    bash "$RESET" restore "$CACHE_SNAPSHOT" || {
      echo "cache restore FAILED — refusing to run arm $arm without isolation" >&2
      exit 3; }
  fi

  if [ "$arm" = "$CHAOS_ARM" ]; then run_arm "$arm" chaos; else run_arm "$arm" control; fi

  if [ "$CACHE_ISOLATION" = "1" ] && [ "$arm" = "$SNAPSHOT_ARM" ]; then
    echo "--- snapshotting the cache as it stands after the recording arm ---"
    bash "$RESET" export "$CACHE_SNAPSHOT" || {
      echo "cache export FAILED after the recording arm — later arms would run "\
           "without isolation" >&2
      exit 3; }
  fi
done

if [ -f "$W2_DIR/arms/$CONTROL_ARM.json" ] && [ -f "$W2_DIR/arms/$CHAOS_ARM.json" ]; then
  echo
  subset_flag=""
  if [ -n "$ONLY_INSTANCES" ] && [ -f "$ONLY_INSTANCES" ]; then
    subset_flag="--clean-subset $ONLY_INSTANCES"
  fi
  ledger_flag=""
  [ "$STEP_LEDGER" = "marker" ] && ledger_flag="--marker-mode"
  # shellcheck disable=SC2086
  "$PY" "$REPO/infra/swebench-gitlab/verify_l2c.py" \
      --control "$CONTROL_ARM" --chaos "$CHAOS_ARM" --w2-dir "$W2_DIR" $subset_flag $ledger_flag \
      | tee "$W2_DIR/logs/verify_${CONTROL_ARM}_vs_${CHAOS_ARM}.txt"
fi
