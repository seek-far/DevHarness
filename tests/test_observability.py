"""Observability instrumentation tests.

Covers two seams from the stress-test plan:

  1. Heartbeat payload — `bf_worker._heartbeat_loop` writes a unix-ms
     timestamp (string-encoded bytes) instead of the old ``b"alive"``
     placeholder. HealthMonitor's TTL liveness check is value-format-
     agnostic, so it MUST keep working with any payload; the new value
     is purely additive observability information. Phase-2 latency can
     then be computed from the difference between spawn_at and the
     heartbeat value with NO 5 s HealthMonitor poll-cadence noise.

  2. ``tools/load_sampler.compute_sample`` — given a Redis stub with a
     known queue depth + heartbeat keys, the sampler returns a row with
     the expected columns, correct active-worker count, sorted bug_id
     CSV, and a heartbeat-age p50/max derived from the unix-ms payload.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))


# ── _heartbeat_loop payload shape ───────────────────────────────────────────


def _load_worker_module():
    """Load bf_worker.py as a module without colliding with the bf_worker
    package's __init__.py (empty file, but Python's import machinery still
    treats it as the canonical ``import bf_worker`` target). Importlib
    direct-file load keeps the package import path untouched."""
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "bf_worker" / "bf_worker.py"
    spec = importlib.util.spec_from_file_location("_bf_worker_entry", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_one_heartbeat_iteration(setex_mock) -> bytes:
    """Drive _heartbeat_loop through exactly one SETEX then cancel.

    The loop body is SETEX → sleep, so the first SETEX fires before any
    awaitable sleep. Cancelling via the sleep mock keeps the test fast.
    """
    worker_mod = _load_worker_module()

    fake_redis = MagicMock()
    fake_redis.setex = AsyncMock(side_effect=setex_mock)

    async def _drive():
        async def _sleep_raises(_):
            raise asyncio.CancelledError
        with patch.object(worker_mod.asyncio, "sleep", _sleep_raises):
            try:
                await worker_mod._heartbeat_loop(fake_redis, "worker:heartbeat:BUG-1")
            except asyncio.CancelledError:
                pass

    asyncio.run(_drive())
    fake_redis.setex.assert_called_once()
    return fake_redis.setex.call_args.args[2]  # third positional = value


def test_heartbeat_value_is_unix_ms_timestamp_bytes():
    """Pins the payload shape: bytes-encoded base-10 unix-ms integer.
    HealthMonitor doesn't read the value, but the load_sampler and any
    phase-2 latency reporter do — if a future refactor changed this to
    JSON or a binary encoding, those consumers would silently break."""
    captured = {}

    def capture(_key, _ttl, value):
        captured["value"] = value

    raw = _run_one_heartbeat_iteration(capture)
    assert isinstance(raw, bytes)
    # Round-trip: the value should parse back to an integer close to "now".
    parsed_ms = int(raw.decode())
    now_ms = time.time_ns() // 1_000_000
    assert abs(now_ms - parsed_ms) < 5_000, (parsed_ms, now_ms)


def test_heartbeat_value_is_not_literal_alive():
    """Explicit guard: the pre-Q1.1 placeholder was ``b"alive"``. If
    someone ever reverts the loop, this test fails before the change
    lands in production."""
    captured = {}

    def capture(_key, _ttl, value):
        captured["value"] = value

    raw = _run_one_heartbeat_iteration(capture)
    assert raw != b"alive"


def test_heartbeat_ttl_preserved():
    """The TTL stays = worker_heartbeat_ttl * 2 (the HealthMonitor liveness
    contract). Observability instrumentation must not change liveness
    semantics by accident."""
    from settings import worker_cfg as cfg

    captured = {}

    def capture(_key, ttl, _value):
        captured["ttl"] = ttl

    _run_one_heartbeat_iteration(capture)
    assert captured["ttl"] == cfg.worker_heartbeat_ttl * 2


# ── load_sampler.compute_sample ─────────────────────────────────────────────


class _FakeRedis:
    """In-memory stand-in covering the three calls compute_sample needs:
    xlen, scan_iter, get. No need for fakeredis — too narrow a surface to
    pull in a new dependency."""

    def __init__(self, queue_depth: int, heartbeats: dict[str, bytes | None]):
        self._queue_depth = queue_depth
        # bug_id → bytes value (or None to model "key exists but value
        # disappeared", e.g. a TTL expiring mid-read).
        self._hbs = heartbeats

    def xlen(self, _key):
        return self._queue_depth

    def scan_iter(self, match=None, count=None):
        # Pattern-match approximation — we only ever pass "worker:heartbeat:*".
        prefix = match.split("*", 1)[0] if match and match.endswith("*") else match
        for bug_id in self._hbs:
            full_key = f"worker:heartbeat:{bug_id}".encode()
            if prefix is None or full_key.decode().startswith(prefix):
                yield full_key

    def get(self, key):
        key_str = key.decode() if isinstance(key, bytes) else key
        bug_id = key_str.split(":", 2)[2]
        return self._hbs[bug_id]


def test_load_sampler_compute_sample_basic_shape():
    from tools import load_sampler

    now_ms = time.time_ns() // 1_000_000
    fake = _FakeRedis(
        queue_depth=3,
        heartbeats={
            "BUG-A": str(now_ms - 50).encode(),    # 50 ms stale
            "BUG-B": str(now_ms - 250).encode(),   # 250 ms stale
            "BUG-C": str(now_ms - 100).encode(),   # 100 ms stale
        },
    )
    sample = load_sampler.compute_sample(fake, "gateway:stream", "worker:heartbeat:*")

    assert sample["queue_depth"] == 3
    assert sample["active_workers"] == 3
    # CSV is sorted by bug_id — joining with phase_marker log lines becomes
    # deterministic this way.
    assert sample["active_workers_csv"] == "BUG-A,BUG-B,BUG-C"
    # p50 of [50, 250, 100] ≈ 100, max = 250 (with a small tolerance for
    # the clock advancing between hb write and sample read in the test).
    assert sample["hb_age_ms_p50"] >= 100
    assert sample["hb_age_ms_max"] >= 250


def test_load_sampler_ignores_legacy_alive_payload():
    """Older workers wrote ``b"alive"`` — a sampler running during a
    rolling upgrade must not crash on it; it should just skip the age
    contribution for that worker and still report active count."""
    from tools import load_sampler

    now_ms = time.time_ns() // 1_000_000
    fake = _FakeRedis(
        queue_depth=0,
        heartbeats={
            "BUG-NEW": str(now_ms - 80).encode(),
            "BUG-OLD": b"alive",
        },
    )
    sample = load_sampler.compute_sample(fake, "gateway:stream", "worker:heartbeat:*")
    assert sample["active_workers"] == 2  # both still alive (key exists)
    # Only the parseable timestamp contributes to the age stats.
    assert sample["hb_age_ms_max"] >= 80


def test_load_sampler_no_workers_returns_empty_age_columns():
    from tools import load_sampler

    fake = _FakeRedis(queue_depth=0, heartbeats={})
    sample = load_sampler.compute_sample(fake, "gateway:stream", "worker:heartbeat:*")
    assert sample["active_workers"] == 0
    assert sample["active_workers_csv"] == ""
    # Empty string keeps the TSV row well-formed without forcing a sentinel
    # int that would skew downstream histograms.
    assert sample["hb_age_ms_p50"] == ""
    assert sample["hb_age_ms_max"] == ""


def test_load_sampler_columns_are_stable():
    """The TSV column list is the contract for downstream parsers. If a
    future change reorders or drops a column, this test trips before it
    breaks any existing log-processing script."""
    from tools import load_sampler

    assert load_sampler.COLUMNS == (
        "t_wall_ms",
        "queue_depth",
        "active_workers",
        "active_workers_csv",
        "hb_age_ms_p50",
        "hb_age_ms_max",
    )
