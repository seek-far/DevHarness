"""Orchestrator Prometheus metrics.

Five counters + two gauges, low-cardinality labels only:

Counters (alerting signals):
  sdlcma_workers_spawned_total{spawner}
      Per-spawner spawn rate. spawner ∈ {process, docker, ecs, k8s} —
      bounded.
  sdlcma_worker_restarts_total{cause}
      HealthMonitor restart attempts. cause ∈ {heartbeat_expired} today;
      extensible. Should stay near 0 in steady state; spikes = real
      worker instability.
  sdlcma_bug_id_collisions_total
      Increments when spawner.spawn() sees an existing registry entry for
      the minted bug_id. Should be 0 forever post-Item-1 (urandom tail
      closed the decisecond-collision window); non-zero = regression.
  sdlcma_dead_letter_total{stream}
      Successful XADDs to a dead-letter stream. Should be 0 in healthy
      runs; non-zero = systemic handler failure.
  sdlcma_spawn_wallclock_ms (histogram, no labels)
      Time from BugReportedEvent receive to spawner.spawn() return.
      Phase-1 + spawner-setup combined; useful for spawner choice
      comparison.

Gauges (capacity / backlog dashboards):
  sdlcma_active_workers{status}
      Count of workers `WorkerRegistry.all_active()` considers in-flight:
      status ∈ {warmup, running}. Mirrors the registry's own active
      definition; `done`/`failed` are deliberately NOT exposed here
      because (a) registry.all_active() filters them out so the gauge
      could never observe them anyway, and (b) the registry currently
      doesn't remove done/failed entries from its underlying dict
      (separate cleanup issue), so a `done` label would grow
      monotonically and look like a leak in the dashboard.
  sdlcma_stream_pending{stream,group}
      Consumer-group backlog (XPENDING total). Real backlog =
      sum across groups; distinct from XLEN (monotonic, not a backlog).
      Sampled by HealthMonitor's existing tick — no extra task.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram


WORKERS_SPAWNED = Counter(
    "sdlcma_workers_spawned_total",
    "Workers spawned by the orchestrator, by spawner kind",
    ["spawner"],
)

WORKER_RESTARTS = Counter(
    "sdlcma_worker_restarts_total",
    "HealthMonitor restart attempts, by cause",
    ["cause"],
)

BUG_ID_COLLISIONS = Counter(
    "sdlcma_bug_id_collisions_total",
    "Spawner saw an existing registry entry for the minted bug_id "
    "(should be 0 post-Item-1)",
)

DEAD_LETTER = Counter(
    "sdlcma_dead_letter_total",
    "Messages routed to a dead-letter stream after handler failure",
    ["stream"],
)

SPAWN_WALLCLOCK_MS = Histogram(
    "sdlcma_spawn_wallclock_ms",
    "Wallclock from BugReportedEvent receive to spawner.spawn() return",
    buckets=(10, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000),
)

ACTIVE_WORKERS = Gauge(
    "sdlcma_active_workers",
    "WorkerRegistry size by status",
    ["status"],
)

STREAM_PENDING = Gauge(
    "sdlcma_stream_pending",
    "Consumer-group PEL size (real backlog, distinct from XLEN)",
    ["stream", "group"],
)
