#!/usr/bin/env python3
"""
tools/load_sampler.py — observability sidecar for stress runs.

While a stress test fires N concurrent webhooks, this script samples Redis
every --interval-ms ticks and writes one TSV row per sample with:

  t_wall_ms             unix-ms wall clock at sample time
  queue_depth           XLEN gateway:stream — work the orchestrator hasn't
                        XREADGROUP'd off yet (post-ACK)
  active_workers        count of keys matching `worker:heartbeat:*` — workers
                        with a still-live heartbeat TTL (the same liveness
                        signal HealthMonitor checks)
  active_workers_csv    comma-separated bug_ids of those workers (for joining
                        with phase_marker log lines)
  hb_age_ms_p50/max     percentiles of (sample_t_wall_ms - heartbeat_value_ms)
                        across all active workers; heartbeat value is the
                        unix-ms timestamp the worker last refreshed. Approxi-
                        mates "how stale are heartbeats right now" — a healthy
                        burst should keep p50 < worker_heartbeat_interval.

Post-process for the four-phase view: join the TSV's t_wall_ms with
phase_marker INFO log lines (grep `phase_marker` across gateway / orchestrator
/ worker stdout) by bug_id. Achieved concurrency = max(active_workers) over
the run; time-weighted average = mean(active_workers) across samples.

Configuration is GitLab-mode-aware via --redis-url; defaults to the
local_multi_process Redis DB so the script works out of the box against the
bundled dev stack. Stops cleanly on SIGINT.

Usage:
  # baseline run, sample every 500ms, write to TSV
  python tools/load_sampler.py --redis-url redis://localhost:6379/1 \\
      --out /tmp/sample-$(date +%s).tsv

  # tighter cadence for a quick burst
  python tools/load_sampler.py --interval-ms 100 --out /tmp/burst.tsv
"""

from __future__ import annotations

import argparse
import signal
import statistics
import sys
import time
from pathlib import Path

import redis


DEFAULT_REDIS_URL = "redis://localhost:6379/1"
DEFAULT_INTERVAL_MS = 500
DEFAULT_GATEWAY_STREAM = "gateway:stream"
DEFAULT_HEARTBEAT_PATTERN = "worker:heartbeat:*"

# TSV columns. Keep this list stable — downstream parsers depend on the
# order. Add new columns at the end.
COLUMNS = (
    "t_wall_ms",
    "queue_depth",
    "active_workers",
    "active_workers_csv",
    "hb_age_ms_p50",
    "hb_age_ms_max",
)


def scan_heartbeats(r: redis.Redis, pattern: str) -> list[tuple[str, bytes | None]]:
    """Return [(bug_id, raw_value)] for every matching heartbeat key.

    SCAN (not KEYS) so a long-running sampler can't stall Redis under load.
    Reads the value too — heartbeat payload is a unix-ms timestamp written
    by bf_worker._heartbeat_loop; consumers compute now - parsed_value to
    get the "how stale" age.
    """
    out: list[tuple[str, bytes | None]] = []
    for key in r.scan_iter(match=pattern, count=100):
        key_str = key.decode() if isinstance(key, bytes) else key
        # key shape: worker:heartbeat:{bug_id}
        bug_id = key_str.split(":", 2)[2] if key_str.count(":") >= 2 else key_str
        try:
            raw = r.get(key)
        except redis.RedisError:
            raw = None
        out.append((bug_id, raw))
    return out


def compute_sample(
    r: redis.Redis, gateway_stream: str, heartbeat_pattern: str
) -> dict:
    t_wall_ms = time.time_ns() // 1_000_000
    try:
        queue_depth = int(r.xlen(gateway_stream))
    except redis.ResponseError:
        # Stream doesn't exist yet (no webhooks landed) — treat as 0.
        queue_depth = 0
    hbs = scan_heartbeats(r, heartbeat_pattern)
    bug_ids = sorted(b for b, _ in hbs)
    ages_ms: list[int] = []
    for _, raw in hbs:
        if raw is None:
            continue
        try:
            hb_ts_ms = int(raw.decode())
        except (ValueError, AttributeError):
            # Legacy "alive" payload (pre-Q1.1) — age is unknown, skip.
            continue
        ages_ms.append(max(0, t_wall_ms - hb_ts_ms))
    return {
        "t_wall_ms": t_wall_ms,
        "queue_depth": queue_depth,
        "active_workers": len(hbs),
        "active_workers_csv": ",".join(bug_ids),
        "hb_age_ms_p50": int(statistics.median(ages_ms)) if ages_ms else "",
        "hb_age_ms_max": max(ages_ms) if ages_ms else "",
    }


def write_row(out, sample: dict) -> None:
    out.write("\t".join(str(sample[c]) for c in COLUMNS) + "\n")
    out.flush()  # tail -f friendliness — sampler stays useful while running


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Stress-test observability sampler")
    p.add_argument("--redis-url", default=DEFAULT_REDIS_URL)
    p.add_argument("--interval-ms", type=int, default=DEFAULT_INTERVAL_MS,
                   help=f"sample cadence in ms (default: {DEFAULT_INTERVAL_MS})")
    p.add_argument("--gateway-stream", default=DEFAULT_GATEWAY_STREAM)
    p.add_argument("--heartbeat-pattern", default=DEFAULT_HEARTBEAT_PATTERN)
    p.add_argument("--out", type=Path, default=None,
                   help="TSV output path (default: stdout)")
    p.add_argument("--duration-s", type=int, default=0,
                   help="stop after N seconds (0 = run until SIGINT)")
    args = p.parse_args(argv)

    r = redis.from_url(args.redis_url, decode_responses=False)

    out = sys.stdout if args.out is None else args.out.open("w", encoding="utf-8")
    out.write("\t".join(COLUMNS) + "\n")
    out.flush()

    stop = {"flag": False}

    def _on_sigint(_sig, _frame):
        stop["flag"] = True
    signal.signal(signal.SIGINT, _on_sigint)
    signal.signal(signal.SIGTERM, _on_sigint)

    interval_s = max(0.01, args.interval_ms / 1000)
    t_end = time.monotonic() + args.duration_s if args.duration_s > 0 else float("inf")

    print(f"# load_sampler: redis={args.redis_url} interval={args.interval_ms}ms "
          f"out={'stdout' if args.out is None else args.out}",
          file=sys.stderr, flush=True)

    samples = 0
    next_tick = time.monotonic()
    while not stop["flag"] and time.monotonic() < t_end:
        sample = compute_sample(r, args.gateway_stream, args.heartbeat_pattern)
        write_row(out, sample)
        samples += 1
        # Pace by absolute targets, not sleep(interval) — keeps the cadence
        # stable even when compute_sample takes a few ms under load.
        next_tick += interval_s
        delay = next_tick - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            # Compute lagged — skip ahead to keep up; don't try to catch up
            # because that just exacerbates the lag.
            next_tick = time.monotonic()

    print(f"# load_sampler: wrote {samples} sample(s)", file=sys.stderr)
    if args.out is not None:
        out.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
