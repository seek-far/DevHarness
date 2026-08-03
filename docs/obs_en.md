# SDLCMA v08 — Observability Design

> This document describes SDLCMA's observability across three axes:
> **Logs (including the performance-oriented `phase_marker` lines) / RunRecord /
> Metrics**, bracketed by a "how the three pillars relate" framing and a
> cross-cutting design-principles section — because the project's observability
> has a few hard invariants that span all three (cardinality, cumulative-vs-rate,
> additive schema evolution).
>
> Chinese sibling lives outside the repo at `D:\PL\sdlcma\obs_cn.md`.
>
> Source: `gateway/metrics.py`, `orchestrator/metrics.py`, `llm_gateway/metrics.py`,
> `bf_worker/agents/run_record.py`, `bf_worker/journal.py`,
> `tools/runrecord_to_metrics.py`, `tools/runrecord_exporter.py`,
> `tools/load_sampler.py`, `tools/analyze_phase_log.py`,
> `infra/grafana/sdlcma_dashboard.json`.

---

## 0. The three pillars and how they relate

SDLCMA's observability is deliberately split into three pillars at different
**granularities / time-scales / audiences** — they do not substitute for one
another:

| Pillar | Granularity | Lifetime | Question it answers | Audience |
|---|---|---|---|---|
| **Logs** | per-event / per-step | process stdout (ephemeral unless externally shipped) | "what exactly happened this run, and which phase did it stall in" | debuggers, stress analysis |
| **RunRecord** | one `agent.fix()` | persisted to `evaluation/journal/` (one `record.json` per bug) | "the full structured outcome of this fix (success, cost, iterations, code version)" | reproducibility, eval comparison, fixture promotion |
| **Metrics** | fleet aggregate | Prometheus time-series (with retention) | "system health / capacity / throughput / cost, now and over time" | dashboards, alerting |

How they join up:

- **Logs → Metrics**: `phase_marker` log lines yield per-bug four-phase latency
  (p50/p95/max); long-running services instead bake the same kind of signal
  straight into Prometheus histograms (e.g. `sdlcma_gateway_handle_ms`). Logs
  are the per-event narrative; metrics are the aggregated distribution.
- **RunRecord → Metrics**: the worker is a **one-shot subprocess** with nowhere
  to keep a live Prometheus counter. So after a RunRecord lands on disk,
  `runrecord_to_metrics.py` (bare-host textfile) or `runrecord_exporter.py`
  (K8s HTTP) **rescans the journal directory** and aggregates it into metrics.
  This is exactly why journal-derived metrics must use cumulative `sum()`, not
  `rate()` (see §4.2).

  ⚠️ **Multi-node blind spot (k3s, as of plan item W4).** The journal is a
  **node-local hostPath** and `runrecord-exporter` is pinned to the server
  node, so a worker that runs on another node writes a RunRecord the exporter
  never sees. `sdlcma_runs_total` and every other journal-derived family are
  therefore *structurally low* on a multi-node cluster — not wrong, but not
  complete either, and nothing about the metric says so. **Do not judge
  cross-node runs from Grafana**; read `ssh <node> ls /var/sdlcma/journal`
  (`infra/k3s/crossnode-check.sh` prints the reminder after a cross-node run).
  Only this family is affected: gateway / orchestrator / llm-gateway metrics
  are in-process counters and those services are all pinned to one node.
  The fix — an exporter per node, whose disjoint journals `sum()` correctly,
  plus `max()` on the scan-timestamp panel so the freshness graph stays one
  line — is W5/W6 work, deliberately kept out of W4.
- **Logs ↔ RunRecord**: the `phase_marker phase=fix_end` line carries
  `outcome`/`iterations`/`elapsed_ms` matching the RunRecord fields of the same
  name (`elapsed_s × 1000`), so they cross-check.

---

## 1. Logs

### 1.1 Structured conventions

Each service uses the stdlib `logging`, with a per-service prefix (gateway
`[gw …]`, orchestrator `[orch …]`, worker `[worker:{bug_id} …]`). Logs carry two
duties:

1. **Narrative logs** — node enter/exit, provider calls, retries, guardrail
   hits, exceptions.
2. **Performance markers (`phase_marker`)** — single-line, machine-parseable,
   forming the spine of stress-test latency analysis.

### 1.2 phase_marker — the latency spine

Every performance marker is a **single line** with a uniform prefix, so one
`grep phase_marker` across the three services rebuilds the timeline:

```
<log prefix> phase_marker phase=<X> key=value key=value ...
```

`key=value` parse rule (`tools/analyze_phase_log.py`'s `_KV_RE`): a phase name
(`worker_ready`, `fix_start`, …) plus `bug_id` / `job_id` / `ref` / `t_wall_ms`
(unix-ms wall clock), etc.

Wired marker points (in data-flow order):

| Phase | Emitted at | Meaning |
|---|---|---|
| `gateway_received` | `gateway/gateway.py:93` | webhook received (carries `job_id`, `ref`) |
| `spawn_start` | `orchestrator/orchestrator.py:171` | begin spawning the worker (carries `bug_id`, `job_id`, `ref`) |
| `worker_imports_done` / `worker_agent_ref_done` | `bf_worker/bf_worker.py` | Python imports done / agent_ref worktree re-exec done |
| `worker_ready` | `bf_worker/bf_worker.py:85` | worker ready (heartbeat started) |
| `worker_llm_probe_done` | `bf_worker/bf_worker.py:157` | self-hosted model-name verification probe done |
| `fix_start` / `fix_end` | `bf_worker/bf_worker.py:168/174` | `agent.fix()` start/end (`fix_end` carries `outcome`, `iterations`) |
| `llm_call` | `react_loop.py:584`, `reflection.py:287` | each LLM call (carries `source=react_loop\|reflection`, `wallclock_ms`) |
| `apply_test_start` / `apply_test_venv_done` / `apply_test_end` | `apply_change_and_test.py` | patch + venv build + test sub-phases (carries `attempt`) |
| `ci_wait_start` / `ci_wait_end` | `wait_ci_result.py` | CI wait start/end (`ci_wait_end` carries `ci_status`, `retries`) |
| `cache_lookup` / `cache_summary` | `llm_gateway/app.py` | LLM gateway cache hit/miss and periodic summary |

### 1.3 The four-phase latency model

`analyze_phase_log.py` joins markers into four phases (cross-service first by
`(job_id, ref)→bug_id`, then by `bug_id` downstream):

1. `gateway_received → spawn_start` — Redis stream queue + consumer poll
2. `spawn_start → worker_ready` — process/container startup + Python init +
   agent_ref re-exec + LLM probe
3. `fix_start → fix_end` — `agent.fix()` wallclock (== `RunRecord.elapsed_s × 1000`)
4. per-`llm_call` `wallclock_ms` (already on the line)

Outputs per-bug phase timings + aggregate p50/p95/max.

### 1.4 Heartbeat timestamp (dual purpose)

Every `worker_heartbeat_interval` seconds the worker `SETEX worker:heartbeat:{bug_id}`,
and **the value is a unix-ms wall clock** (`bf_worker/bf_worker.py:62`):

- HealthMonitor only checks **whether the TTL expired** (liveness) — it **ignores
  the value**.
- `load_sampler.py` / stress analysis read the **value** and compute
  `hb_age_ms = sample_time - heartbeat_value`, giving a "how stale are heartbeats
  right now" distribution (a healthy burst keeps p50 < `worker_heartbeat_interval`).

### 1.5 Stress-test observability tooling

| Tool | Role |
|---|---|
| `tools/load_sampler.py` | Stress sidecar sampler: every `--interval-ms`, sample Redis, one TSV row each: `stream_total` (XLEN, monotonic NOT backlog), `consumer_pending` (XPENDING, delivered-unACKed), `consumer_lag` (XINFO GROUPS lag, undelivered), `active_workers` (`worker:heartbeat:*` count), `hb_age_ms_p50/max`. **True backlog = pending + lag.** |
| `tools/analyze_phase_log.py` | Post-processor: join phase_marker streams (optionally overlay the sampler TSV) → four-phase latency + achieved concurrency (`max(active_workers)`) + max queue depth + heartbeat freshness. |
| `tools/trigger_concurrent_pipelines.py` | Stress generator: fire N concurrent pipelines. |

> Key distinction (`load_sampler.py` header): **XLEN is a monotonic cumulative
> count, NOT a backlog** (Redis Streams don't auto-trim on ACK); the real backlog
> is `consumer_pending + consumer_lag`.

---

## 2. RunRecord — the canonical per-run structure

### 2.1 Role

`RunRecord` (`bf_worker/agents/run_record.py`) is the **canonical structured
outcome of one `agent.fix()` invocation**, used in two places that read and
write the same shape so downstream tooling (metrics, promotion, external
dashboards) handles exactly one schema:

- `bf_worker/journal.py` — running-mode auto-capture (always on)
- `evaluation/runner.py` — one cell per eval sweep

### 2.2 SCHEMA_VERSION and additive evolution (project invariant #5)

`SCHEMA_VERSION = "1"`. **Fields are additive-only**: a new field gets a default
and stays backward-compatible (older records missing it read as `None`, treated
as zero or skipped). **Bumping `SCHEMA_VERSION` is the trigger** — it must be
mirrored to `CLAUDE.md` / `AGENTS.md` / `README.md` in the same commit
(downstream metrics, promotion, external dashboards depend on it;
reproducibility-critical).

### 2.3 Field groups

| Group | Fields (excerpt) | Purpose |
|---|---|---|
| **Core / outcome** | `schema_version`, `agent_name`, `bug_id`, `outcome` (`fixed`/`no_fix`/`error`/`already_fixed`), `timestamp`, `error`, `iterations`, `elapsed_s` | success, iterations, wallclock |
| **Identity / context** | `project_id`, `project_web_url`, `job_id`, `agent_config`, `run_id` (eval only), `llm_model` | attribution |
| **Agent code version** | `agent_code_git_{commit,branch,dirty,status}` | reproducibility — records the SDLCMA checkout that ran (incl. dirty-state snapshot, `git status --short` capped at 4000 chars) |
| **Graph telemetry** | `react_step_count`, `react_confidence`, `fix_branch_name`, `branch_create_status`, `base_branch/commit`, `commit_status/hash`, `review_status/url/iid`, `test_passed`, `suspect_file_path`, `parse_trace_fallback`, `source_fetch_failed` | node-level behaviour; `parse_trace_fallback` is the parser-regression signal |
| **Transient retry counts** | `fetch_trace_retries`, `fetch_source_file_retries`, `commit_change_retries`, `wait_ci_result_retries`, `create_mr_retries` | per-I/O-node transient retries |
| **LLM cost / latency** (additive) | `llm_call_count`, `total_prompt_tokens`, `total_completion_tokens`, `total_cached_input_tokens`, `total_llm_wallclock_s`, `llm_call_wallclock_ms` (per-call ms list → p50/p95/p99), `max_input_tokens` | cost driver; all `None` on R10 short-circuit; `total_cached_input_tokens` stays `None` on backends not reporting caching (distinct from `0` = reported-but-nothing-cached) |
| **Model / backend identity** | `llm_model_served` (vLLM `/v1/models` actually-served name, self-hosted only, for post-hoc detection of silent mismatch), `llm_backend_name` (the backend the LLM gateway routed to, distinct from the model id) | same model name across backends still aggregates per-backend |
| **Code Review (Phase 2)** | `code_review_status`, `code_review_finding_count`, `code_review_would_escalate`, `code_review_findings`, `code_review_rounds` | reviewer-node telemetry |
| **Reflection** | `reflection_count`, `reflection_mode` (`apply` deterministic / `test` LLM post-mortem) | enhancement A/B |
| **no_fix retry** | `no_fix_retry_count` (gateway-only escalation path, capped `NO_FIX_MAX_RETRIES=1`) | 0/1/2 tri-state |

### 2.4 Journal directory layout (`bf_worker/journal.py`)

Each run writes one directory `{ts}_{bug_id}_{agent}[_{model_slug}]/`:

- `record.json` — the serialized RunRecord (canonical schema)
- `trace.txt` — captured failure trace (if present)
- `test_output.txt` — pytest output from the last apply+test step
- `llm_result.json` — the LLM's final structured fix proposal
- `FLAGGED` — sentinel marking a run worth promoting to a fixture (failed /
  no_fix / high-iteration success)

`model_slug`: non-`[A-Za-z0-9._-]` → `-`, capped at 60 chars (so slash-separated
vendor-prefixed model IDs don't create stray subdirectories). The journal is
always-on and cheap: you can't tell a bug is "interesting" until it plays out, so
capture everything and curate later via `bench promote` (a curation gate, not
auto-promotion).

---

## 3. Metrics — live export

The three long-running services each expose `/metrics` (Prometheus text format),
**all with low-cardinality labels only** — production-safe at any project count.

### 3.1 Gateway (`gateway/metrics.py`, `GET /metrics` on the uvicorn port)

Explicit route (not `app.mount`, which 307-redirects `/metrics`→`/metrics/`).

| Metric | Type | Labels | Notes |
|---|---|---|---|
| `sdlcma_webhooks_received_total` | Counter | `object_kind`, `classification` | every POST /webhook; `classification ∈ {bug_reported, validation, other, invalid}` (mirrors the parser decision tree, splitting real bugs from our-own-push noise) |
| `sdlcma_webhooks_forwarded_total` | Counter | — | successful XADDs to `gateway:stream`; delta vs received = Redis-layer failures (hard alert) |
| `sdlcma_gateway_handle_ms` | Histogram | — | receive → XADD complete wallclock; buckets tuned tight-low (p50~1ms, p95~2ms) with a wide tail |

### 3.2 Orchestrator (`orchestrator/metrics.py`, dedicated `METRICS_PORT`, default 9102)

`start_http_server(metrics_port, addr=METRICS_BIND)`; `METRICS_PORT=0` disables
(unit tests / standalone); a bind failure only logs and does not block startup
("metrics off is better than no orchestrator").

| Metric | Type | Labels | Notes |
|---|---|---|---|
| `sdlcma_workers_spawned_total` | Counter | `spawner` (`process`/`docker`/`ecs`/`k8s`) | per-spawner spawn rate |
| `sdlcma_worker_restarts_total` | Counter | `cause` (today `heartbeat_expired`) | ~0 in steady state, spikes = worker instability |
| `sdlcma_bug_id_collisions_total` | Counter | — | spawn saw an existing registry entry; should be 0 forever post-Item-1 (urandom tail), non-zero = regression |
| `sdlcma_dead_letter_total` | Counter | `stream` | dead-letter XADDs; 0 in healthy runs |
| `sdlcma_spawn_wallclock_ms` | Histogram | — | BugReportedEvent receive → `spawn()` return |
| `sdlcma_active_workers` | Gauge | `status` (`warmup`/`running`) | mirrors the registry's in-flight definition; **deliberately omits done/failed** (registry doesn't evict them, so a `done` label would grow monotonically and look like a leak) |
| `sdlcma_stream_pending` | Gauge | `stream`, `group` | consumer-group PEL (real backlog, distinct from XLEN); sampled by the HealthMonitor's existing tick |

### 3.3 LLM Gateway (`llm_gateway/metrics.py`, `GET /metrics`)

| Metric | Type | Labels | Notes |
|---|---|---|---|
| `sdlcma_llm_cache_lookups_total` | Counter | `result` (`hit`/`miss`/`disabled`), `mode` (`disabled`/`record`/`replay`/`cache`) | once per request; hit rate = `hit / total`, tokens saved ∝ hit rate |
| `sdlcma_llm_upstream_wallclock_ms` | Histogram | `backend` | gateway's wait on the backend; **cache hits NOT observed** (they bypass upstream) |

> Cardinality bounded by config: `mode` is 4 values, `backend` is typically 1–3,
> no per-bug / per-model leaks.

### 3.4 Journal-derived metrics (two deployment shapes, one codebase)

The worker is one-shot with nowhere to keep a live counter → a separate process
**rescans the journal** and aggregates. Both shapes share
`runrecord_to_metrics.py`'s `_scan_journal` / `_aggregate` / `render`, so the
**metric names, labels, and values are identical — switching shapes changes
nothing**:

| Shape | Tool | Mechanism |
|---|---|---|
| Bare host | `tools/runrecord_to_metrics.py` | cron periodically writes a `.prom` file; node_exporter textfile collector scrapes it; atomic write (`.tmp` + rename) prevents half-writes |
| K8s | `tools/runrecord_exporter.py` | long-running HTTP; every `GET /metrics` rescans+aggregates fresh; freshness governed by the Prometheus scrape interval; journal mounted RO via hostPath; a scrape exception returns 500 so `up{}=0` surfaces the outage |

Derived metric families:

| Metric | Labels | Notes |
|---|---|---|
| `sdlcma_runs_total` | `agent`, `model_slug`, `outcome` | RunRecord count (completed runs, success or not) |
| `sdlcma_run_elapsed_seconds_total` | same | sum of `elapsed_s`; ÷ runs_total = mean wallclock |
| `sdlcma_llm_tokens_total` | `type` (prompt/completion/cached), `model_slug` | sum of tokens (cost driver) |
| `sdlcma_llm_calls_total` | `agent`, `model_slug` | sum of `llm_call_count` |
| `sdlcma_parse_trace_fallback_total` | `agent` | count of `parse_trace_fallback=True` (parser-regression signal; was 100% on GitLab-wrapped traces before the 2026-05-29 parser fix) |
| `sdlcma_reflection_fires_total` | `agent` | count of `reflection_count ≥ 1` |
| `sdlcma_runrecord_last_scan_timestamp` | — | Gauge, unix-s of last successful scan; stale value = scanner cron broken |

`model_slug` uses the same slugify as the journal (slash→dash, cap 60) so a
misconfigured URL/path model name can't blow up cardinality.

### 3.5 Grafana dashboard (`infra/grafana/sdlcma_dashboard.json`, seven rows)

Chart-mirrored at `infra/helm/sdlcma/dashboards/`; drift guarded by
`tests/test_grafana_dashboard.py`.

1. **Health (should stay near zero)**: bug_id collisions / dead-letter rate (5m) / worker restarts (5m) / RunRecord scan age
2. **Capacity & backlog**: active workers by status / stream pending (real backlog)
3. **Throughput**: webhooks received by classification (1m rate) / fixes completed by outcome (cumulative)
4. **Latency**: gateway handle p50/p95 / LLM upstream wallclock p50/p95 by backend
5. **LLM cost & cache**: tokens by type (cumulative) / cache hit rate (5m) / cache lookups by result (5m rate) / tokens per fix (avg)
6. **Quality**: fix success rate (cumulative) / parse_trace_fallback (cumulative)
7. **System resources (node-exporter)**: node CPU% / memory total vs used / memory%

---

## 4. Cross-cutting design principles (spanning all three pillars)

### 4.1 Cardinality rule: NEVER tag by `bug_id` or `project_id`

Every metric (live + journal-derived) uses only bounded-enum labels (agent,
outcome, spawner, backend, cache mode, token type, model_slug capped at 60).
Tagging by bug_id/project_id would unboundedly explode Prometheus series. This
holds across gateway / orchestrator / llm_gateway / both exporters.

### 4.2 Cumulative-vs-rate rule: journal-derived panels use `sum()`/ratios, NOT `rate()`/`increase()`

The exporter **recomputes totals from the journal on every scrape**, so the
counter is **born-at-value**, not incremented over time. A `rate()` window over
sparse fixes collapses to ~0. So journal-derived panels (fix counts, tokens,
success rate, …) use cumulative `sum()` / ratios. **Live-service metrics**
(gateway/orchestrator/llm_gateway counters) increment for real inside a
long-running process and use `rate()` normally (e.g. 5m webhook rate, cache
lookup rate). Pinned by `tests/test_grafana_dashboard.py`.

### 4.3 RunRecord additive evolution (invariant #5)

See §2.2. Additive-only fields; a `SCHEMA_VERSION` bump triggers the multi-surface
mirror.

### 4.4 Cross-component dependency packaging (real K8s incident)

Cross-cutting deps like `prometheus-client` must be added to **every** importing
component's `<component>/requirements.txt` (orchestrator/gateway/llm_gateway),
NOT the top-level one — each image installs only its own. The runrecord-exporter
reuses the bf-worker image, so `tools/` must be `COPY`d into
`Dockerfile.bf-worker`. Pinned by `tests/test_image_dependencies.py`. Detail in
`docs/k8s.md` §8.4.

### 4.5 Graceful degradation

A metrics-port bind failure only logs and doesn't block (orchestrator); the
exporter returns 500 on a bad journal entry rather than poisoning the scrape;
journal-write exceptions are swallowed and never break the core run.
Observability must never take down the system it observes.

---

## 5. Wiring on Kubernetes (pointer)

When `monitoring.enabled=true`, the Helm chart pulls in kube-prometheus-stack,
renders ServiceMonitors targeting gateway/orchestrator/llm-gateway/runrecord-exporter,
and ships the SDLCMA dashboard as a labelled ConfigMap (Grafana sidecar
auto-discovers it). The two silent-failure gotchas (ServiceMonitor selector
`NilUsesHelmValues=false`, NetworkPolicy blackhole) are in **`docs/k8s.md` §8**.

---

## 6. Test pointers

- `tests/test_grafana_dashboard.py` — pins journal panels to sum()/ratios not rate(), and the chart-mirror not drifting
- `tests/test_image_dependencies.py` — pins prometheus-client cross-component packaging + `tools/` COPY
- per-service metrics unit tests for gateway/orchestrator/llm_gateway (BEFORE/AFTER deltas, working around prometheus_client's process-wide default registry leak)
- Project memory: `project_k8s_observability_wiring`, `project_k8s_per_component_requirements`, `project_orchestrator_bug_id_race`
