"""SQLite cache store tests.

The store IS the persistence + portability contract. Specifically:
  * put/get round-trip is byte-exact (response bodies are JSON we mustn't
    re-serialize, lest whitespace/key-order drift across replays).
  * hit_count increments on every get(), AND survives close+reopen — the
    durable counter is on the row, not in process memory.
  * Stats counters separate session (process-local) from durable (per-row
    hit_count); /cache/stats can surface both.
  * Opening a sqlite written by a newer schema_version aborts loudly.

The "copy to another machine" property reduces to "the file is a single
sqlite db" — proven by the tests that close, copy the path, reopen
elsewhere, and read entries written before the move.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm_gateway.cache import Cache, SCHEMA_VERSION


def test_put_get_roundtrip(tmp_path):
    c = Cache(tmp_path / "c.db")
    body = b'{"choices":[{"message":{"content":"hi"}}]}'
    c.put("k1", body, 200, 1234, "backend-A")
    e = c.get("k1")
    assert e is not None
    assert e.response_body == body
    assert e.response_status_code == 200
    assert e.original_wallclock_ms == 1234
    assert e.original_backend_name == "backend-A"


def test_get_miss_returns_none_and_bumps_misses(tmp_path):
    c = Cache(tmp_path / "c.db")
    assert c.get("missing") is None
    assert c.stats.misses == 1
    assert c.stats.hits == 0


def test_hit_count_increments_per_get(tmp_path):
    """Every get() bumps both the in-process `hits` counter AND the
    durable per-row `hit_count`. The row counter is what survives
    process restart — the proof point of "persistent stats"."""
    c = Cache(tmp_path / "c.db")
    c.put("k", b"body", 200, 100, "B")
    for _ in range(3):
        e = c.get("k")
        assert e is not None
    assert c.stats.hits == 3
    # Final row hit_count = 3 (incremented in-place on every get).
    assert c.get("k").hit_count == 4   # 4 = 3 prior + this one


def test_hit_count_persists_across_reopen(tmp_path):
    """The durable counter is on the row, so closing and reopening must
    not reset it. This is what makes a 72h soak's hit-rate trustworthy
    even when the gateway gets restarted."""
    db = tmp_path / "c.db"
    c = Cache(db)
    c.put("k", b"body", 200, 100, "B")
    c.get("k"); c.get("k")
    c.close()

    c2 = Cache(db)
    assert c2.stats.hits == 0       # session counter reset on reopen
    e = c2.get("k")
    assert e is not None
    # hit_count from the previous session is preserved + 1 for this get.
    assert e.hit_count == 3


def test_session_counters_separate_from_durable(tmp_path):
    c = Cache(tmp_path / "c.db")
    c.put("k", b"x", 200, 1, "B")
    c.get("k")            # +1 hit
    c.get("k")            # +1 hit
    c.get("missing")      # +1 miss
    snap = c.snapshot_stats()
    assert snap.hits == 2
    assert snap.misses == 1
    assert snap.records == 1
    assert snap.hit_rate == pytest.approx(2 / 3)


def test_reset_session_counters_keeps_durable(tmp_path):
    c = Cache(tmp_path / "c.db")
    c.put("k", b"x", 200, 1, "B")
    c.get("k"); c.get("k")
    c.reset_session_counters()
    snap = c.snapshot_stats()
    assert snap.hits == 0 and snap.misses == 0
    # But the row's hit_count was 2 before the reset.
    e = c.get("k")
    assert e.hit_count == 3


def test_put_upsert_preserves_hit_count_exact(tmp_path):
    """Tighter version of the above test that asserts the exact count
    without ambiguity. After 2 gets + 1 re-record (which keeps the
    counter) + 1 final get → hit_count must be 3."""
    c = Cache(tmp_path / "c.db")
    c.put("k", b"v1", 200, 100, "B-old")
    c.get("k"); c.get("k")
    c.put("k", b"v2", 200, 555, "B-new")
    e = c.get("k")
    assert e.hit_count == 3


def test_copyable_across_machines_emulation(tmp_path):
    """Smoke for the "copy the .db to another machine" workflow: write
    entries on one path, shutil.copy to another, open the second as a
    fresh Cache, read entries. If this passes, scp/rsync do too."""
    src_db = tmp_path / "src.db"
    src = Cache(src_db)
    src.put("k1", b"body-1", 200, 100, "B")
    src.put("k2", b"body-2", 200, 200, "B")
    src.get("k1"); src.get("k1"); src.get("k2")
    src.close()

    dst_db = tmp_path / "dst.db"
    shutil.copy(src_db, dst_db)

    dst = Cache(dst_db)
    e1 = dst.get("k1")
    e2 = dst.get("k2")
    assert e1.response_body == b"body-1"
    assert e2.response_body == b"body-2"
    # Original hit_counts came along for the ride.
    assert e1.hit_count == 3   # 2 hits before move + 1 just now
    assert e2.hit_count == 2   # 1 hit before move + 1 just now


def test_schema_version_mismatch_aborts(tmp_path):
    """A cache file from a future code version must not be silently read
    — better to fail loudly so the user re-records than to corrupt the
    response shape with stale data."""
    db = tmp_path / "c.db"
    c = Cache(db)
    c.close()
    # Force a mismatch by writing a bogus schema_version.
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE meta SET value = ? WHERE key = 'schema_version'",
        ("99",),
    )
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="schema mismatch"):
        Cache(db)


def test_snapshot_stats_includes_entry_count_and_db_size(tmp_path):
    c = Cache(tmp_path / "c.db")
    c.put("k1", b"body-1", 200, 100, "B")
    c.put("k2", b"body-2", 200, 200, "B")
    snap = c.snapshot_stats()
    assert snap.entry_count == 2
    assert snap.db_size_bytes > 0
    assert snap.db_path.endswith("c.db")
    assert snap.schema_version == SCHEMA_VERSION
