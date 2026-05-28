# Stress test #1 — N=19 concurrent burst on WSL, local_multi_process

2026-05-28. First end-to-end stress test of the four-phase observability
layer (`phase_marker` logs + `tools/load_sampler` + `tools/analyze_phase_log`).

## Environment

| | |
|---|---|
| Host | Windows 11 + WSL2, Ubuntu 22.04.5 LTS |
| Kernel | `6.6.114.1-microsoft-standard-WSL2` |
| CPU | 12th Gen Intel Core i7-12650H — 16 logical (8 physical, SMT) |
| Memory | 16 GiB total (13 GiB available at test start), 4 GiB swap |
| Disk | WSL2 ext4 on Windows NTFS (no special tuning) |
| Python | 3.10.12, `.venv-linux` via `uv` |
| Backend ENV | `local_multi_process` — Redis on `localhost:6379/1`, gateway + orchestrator + workers all as host processes |
| GitLab | Self-hosted Omnibus on `minus:8929` (Docker on the WSL host) |
| LLM | Alibaba DashScope `qwen3-coder-480b-a35b-instruct` (cloud API; not local vLLM) |
| Concurrency target | 19 (all bundled fixtures × `--concurrency 19`) |
| `worker_heartbeat_interval` | 10 s (default) |
| `worker_heartbeat_ttl` | 30 s (key TTL written = 2× = 60 s) |
| `health_check_interval` | 20 s |

## Procedure (reproducible)

```bash
# 1) Make sure Redis, the orchestrator's GitLab, and the LLM API are reachable.
source .venv-linux/bin/activate

# 2) Bring up the stack with stdout captured to log files. In
#    local_multi_process mode the worker is spawned as a subprocess and
#    inherits the orchestrator's stdout, so worker phase_marker lines land
#    in orchestrator.log too.
uv run uvicorn gateway.gateway:app --host 0.0.0.0 --port 8000 \
    >/tmp/gateway.log 2>&1 &
uv run python -m orchestrator.orchestrator \
    >/tmp/orchestrator.log 2>&1 &

# 3) Start the Redis sidecar sampler. Must use the SAME --redis-url the
#    orchestrator uses (db=1 for local_multi_process); a mismatch makes
#    active_workers/queue_depth all zero (silent miscompare).
uv run python tools/load_sampler.py \
    --redis-url redis://localhost:6379/1 \
    --interval-ms 500 \
    --out /tmp/burst.tsv &

# 4) Fire the burst. --concurrency must equal len(fixtures) for a TRUE
#    simultaneous burst (ThreadPoolExecutor pace would otherwise serialise).
uv run python tools/trigger_concurrent_pipelines.py \
    --gitlab-url http://minus:8929 --token <token> \
    --namespace root \
    --fixtures all --concurrency 19

# 5) Wait until N `phase=fix_end` lines land in the orchestrator log.
until [ "$(grep -c 'phase=fix_end' /tmp/orchestrator.log)" -ge 19 ]; do
    sleep 2
done
# Then Ctrl-C the sampler.

# 6) Analyse.
uv run python tools/analyze_phase_log.py \
    --log /tmp/gateway.log --log /tmp/orchestrator.log \
    --sampler /tmp/burst.tsv
```

## Results

### Per-bug latency (top-level)

| Phase | n | p50 | p95 | max |
|---|---|---|---|---|
| `phase1` gateway → spawn | 19 | **1.0 ms** | 2.0 ms | 2 ms |
| `phase2` spawn → fix_start | 19 | **17 263 ms** | 18 936 ms | 19 594 ms |
| `phase3` fix() wallclock | 19 | **72 809 ms** | 76 943 ms | 79 461 ms |
| `llm_call` per call | 61 | **3 525 ms** | 7 646 ms | 10 935 ms |

### Phase-2 sub-breakdown (spawn_start → fix_start)

| Sub-phase | p50 | p95 | max |
|---|---|---|---|
| `cold_python` (interpreter + library eager-load) | **17 105 ms** | 18 676 ms | 19 391 ms |
| `agent_ref_reexec` | 0 ms | 1 ms | 1 ms |
| `asyncio+redis` | 1 ms | 2 ms | 2 ms |
| `llm_probe` (`GET /v1/models`) | 0 ms | 1 ms | 1 ms |
| `make_agent` (LangGraph compile) | 168 ms | 376 ms | 391 ms |

### Phase-3 sub-breakdown (inside `agent.fix()`)

| Sub-phase | p50 | p95 | max |
|---|---|---|---|
| `apply_test` per attempt | 4 792 ms | 5 308 ms | 5 337 ms |
| &nbsp;&nbsp;&nbsp;&nbsp;└ venv + pip install | **4 630 ms** | 5 090 ms | 5 116 ms |
| &nbsp;&nbsp;&nbsp;&nbsp;└ pytest run | 181 ms | 224 ms | 233 ms |
| `apply_test` per-bug sum | 4 792 ms | 5 308 ms | 5 337 ms |
| attempts per bug | 1 | 1 | 1 |
| `ci_wait` per bug | **46 980 ms** | 50 089 ms | 50 447 ms |

### Achieved concurrency (sampler)

| | |
|---|---|
| samples | 671 over the test window |
| `active_workers` max | **19** (target met) |
| `active_workers` mean / time-weighted mean | 3.9 / 3.9 (low because the sample window includes the long tail of CI waits where most workers are idle) |
| `queue_depth` max / mean | 313 / 284 (see "caveat" below) |
| `hb_age_ms` p50 / max | 4 772 ms / 10 000 ms — well within `worker_heartbeat_interval=10s` |

### Outcomes

19 / 19 fixed. 0 errors. 0 HealthMonitor false-positive restarts.

## Findings

### 1. Phase 1 is a non-bottleneck (1 ms p95)

Gateway → orchestrator → spawn takes ~1 ms even when 19 webhooks arrive
within the same wall-clock second. Redis stream + consumer-group loop
keeps up easily.

### 2. Phase 2 = cold Python imports (17 s)

`cold_python` is **99 %** of phase 2. The other sub-phases are noise:

- `llm_probe` is 0 ms because the LLM backend is **cloud DashScope**
  (cached HTTPS connection or skipped entirely). Self-hosted vLLM would
  show non-zero here.
- `make_agent` (370 ms p95) is the LangGraph compile — non-trivial but
  not the headline cost.
- `agent_ref_reexec` ≈ 0 ms as expected (no `agent_ref` pinned).

**17 s for `import` under 19 parallel workers** on a 16-thread machine
in WSL2-on-NTFS is plausible: each worker re-parses `langgraph`,
`openai`, `langchain`, `pydantic`, `requests`, etc. from cold disk. The
filesystem is the bottleneck — Windows NTFS via 9P / virtio is slow for
many-small-file workloads, and 19 processes faulting in the same
~hundreds of `.pyc` files at once amplifies it.

Mitigations (not applied in this test):

- Pre-import in a parent process and `os.fork()` (POSIX only); workers
  inherit ready-loaded modules → ~constant cost regardless of N.
- Warm the page cache before the burst (`find .venv-linux -name "*.pyc" -exec cat {} > /dev/null \;`).
- Move the project off `/mnt/d/` onto WSL-native `~/` storage (ext4 only).
- Switch to Docker spawner with a pre-built image — `cold_python`
  becomes one-time-per-image at build time.

### 3. Phase 3 is dominated by CI wait, not by local work

| Sub-phase | Median | % of phase 3 |
|---|---|---|
| `ci_wait` | 47.0 s | **65 %** |
| LLM calls (≈ 3 × 3.5 s median) | ~10.5 s | 15 % |
| `apply_test` (venv install + pytest) | 4.8 s | 7 % |
| Other (orchestration, push, MR create) | ~10 s | 14 % |

`ci_wait` is GitLab Runner's time — schedule + pull + venv + pip
install + pytest in a fresh container. Since the local `apply_test`
already runs pytest in 0.18 s, the gap is GitLab's container startup
and dependency install. **Mitigating phase 3 is mostly a GitLab-Runner
question, not a worker question.** A long-lived runner image with
`requirements.txt` pre-installed would collapse this dramatically.

### 4. `venv + pip install` is 96 % of `apply_test` (4.6 s of 4.8 s)

Each retry attempt re-creates the venv from scratch. Caching the venv
across attempts within the same `fix()` would halve `apply_test`
wallclock on multi-attempt fixes. Not a problem on this run (every fix
landed first-try) but a known multiplier for harder bugs.

### 5. `queue_depth` of 313 was misleading (now fixed)

The original `queue_depth` column was `XLEN gateway:stream` — total
entries ever XADD'd, not consumer-group backlog. Validation events and
prior-test residue inflated the number; the 313 looked alarming but was
just stream-history accumulation. **Fixed 2026-05-28**: the sampler now
writes three separate columns — `stream_total` (XLEN, monotonic
throughput proxy), `consumer_pending` (XPENDING), `consumer_lag`
(XINFO GROUPS lag). The analyzer reports real backlog =
`consumer_pending + consumer_lag`. On a healthy run both should stay
~0; `stream_total` deltas are throughput, not a backlog metric.

### 6. LLM call distribution is healthy

- 61 calls / 19 bugs = 3.2 calls per bug average (3, occasionally 4–5).
- p50 = 3.5 s, p95 = 7.6 s, max = 10.9 s. p95 / p50 = 2.2 — typical
  long-tail for a large hosted model under concurrent load.
- No call hit the 60-second `LLM_REQUEST_TIMEOUT`, so no spurious
  timeouts contaminated phase 3.

### 7. Heartbeat semantics check out

`hb_age_ms` p50 = 4.8 s, max = 10.0 s. With `worker_heartbeat_interval = 10 s`
the expected range is `[0, 10s)`. The sampler caught one worker right
at the next-refresh boundary — that's the upper bound of the
distribution, not a stress symptom.

## Recommendations (priority-ordered)

1. ~~**Fix the `queue_depth` semantic** in `load_sampler`.~~ Done
   2026-05-28: split into `stream_total` / `consumer_pending` /
   `consumer_lag`, with backlog = pending + lag as the headline.
2. **Cache `venv` across `apply_test` attempts** within one `fix()` —
   re-use the venv if `requirements.txt` hasn't changed. Saves ~4 s per
   retry on multi-attempt fixes.
3. **For real cold-start reduction**, the highest-ROI change is moving
   the project to native ext4 (`~/sdlcma` not `/mnt/d/...`) — would
   likely cut `cold_python` by 2-5×.
4. **For productionising**, switch to the Docker spawner under heavy
   load: `cold_python` becomes a fixed image cost, not a per-spawn cost.

## Raw data

Logs and sampler TSV from this run are in `/tmp/` (not committed). The
exact `analyze_phase_log` output is reproduced at the top of this file's
**Results** section. To re-run with these inputs:

```bash
uv run python tools/analyze_phase_log.py \
    --log /tmp/gateway.log --log /tmp/orchestrator.log \
    --sampler /tmp/burst.tsv
```
