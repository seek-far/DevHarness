# SDLCMA Grafana dashboard

`sdlcma_dashboard.json` is the starting-point Grafana dashboard for the
metrics exposed by:

- **gateway** (`/metrics` on its uvicorn port, default 8000) — Steps 1's
  webhook / forwarded / handle-ms metrics
- **orchestrator** (`/metrics` on `METRICS_PORT`, default 9102) —
  Step 1's spawned / restarts / collisions / dead-letter / active /
  stream-pending metrics
- **llm_gateway** (`/metrics` on its uvicorn port, default 9000) —
  Step 2's cache lookups + upstream wallclock
- **runrecord_to_metrics** (Prometheus textfile collector via cron) —
  Step 3's per-fix fixes / tokens / calls / parse-trace-fallback /
  reflection-fires

The dashboard expects all four Prometheus scrape targets to land in the
same Prometheus instance. Each panel uses a templated `$datasource`
variable so you don't have to hard-code the data-source name.

## Import (UI)

1. In Grafana: **Dashboards** → **New** → **Import**
2. Paste the JSON file's contents (or upload it)
3. Pick your Prometheus data source
4. Click **Import**

## Import (provisioning, automated)

Drop the file into Grafana's dashboard provisioning directory and add a
provider config — example for Grafana 10+:

```yaml
# /etc/grafana/provisioning/dashboards/sdlcma.yaml
apiVersion: 1
providers:
  - name: SDLCMA
    folder: SDLCMA
    type: file
    options:
      path: /etc/grafana/dashboards/sdlcma
```

Then:

```bash
sudo mkdir -p /etc/grafana/dashboards/sdlcma
sudo cp sdlcma_dashboard.json /etc/grafana/dashboards/sdlcma/
sudo systemctl reload grafana-server
```

## Panel layout

The dashboard is grouped into six rows by purpose. Read top-down:

| Row | What it shows | Alert when |
|---|---|---|
| Health | bug_id collisions, dead-letter rate, worker restart rate, RunRecord scrape age | any non-zero or scrape > 5 min stale |
| Capacity & backlog | active workers by status, stream pending | backlog growing |
| Throughput | webhook rate by classification, fix rate by outcome | drop in `bug_reported` rate, spike in `error` outcome |
| Latency | gateway handle p50/p95, LLM upstream p50/p95 by backend | p95 climbs |
| LLM cost & cache | tokens by type, cache hit rate, tokens per fix | cache hit rate < 30 % when expected high; tokens per fix climbs |
| Quality | fix_rate, parse_trace_fallback rate, reflection fires | fix_rate < 0.85; parse_trace_fallback non-zero |

## Tweaking

The supplied PromQL uses 1-minute and 5-minute windows. For
low-traffic dev installs (a few fixes per hour) you'll want to widen
these to `[15m]` or `[1h]` for less noise. The `time` default is
`now-1h`; for a daily review widen to `now-24h`.

Cache hit-rate and fix-rate stat panels use `clamp_min(..., 1)` to
keep them sane on cold-start (no denominator). Don't remove those.

## What's NOT in this dashboard

- Per-bug detail — that lives in the journal (`evaluation/journal/`)
  and the phase_marker logs. Metrics are aggregate; for "why did
  bug X fail" go to the journal.
- Container-level CPU / memory — that's node_exporter or cAdvisor
  territory, separate dashboard.
- Cost in USD — `sdlcma_llm_tokens_total` × your model's per-token
  price is the formula; add a Stat panel with `* <price>` if you want
  USD on the dashboard.
