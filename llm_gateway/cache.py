"""SQLite-backed persistent cache for LLM gateway responses.

One sqlite file = the entire cache. Trivially copyable to other machines
(`cp` / `scp` / `rsync`) — that's the deployment model: record on box A,
ship the .db to box B, replay there for zero-token stress tests.

Schema versioned via a `meta` table; opening a file with an
incompatible schema_version aborts loudly rather than silently
corrupting reads.

Thread-safety: SQLite connection is created lazily per thread (sqlite3
is checked-thread-safe by default). The gateway runs on one asyncio
loop, so all DB calls happen on the main thread — but we don't lean on
that, in case a future tester spins this up off-thread.

Stats: in-memory counters (`hits`, `misses`, `records`, `errors`) plus
on-row `hit_count`. The in-memory ones are the live "this session"
view; the row's `hit_count` is the cumulative across all sessions.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .keying import short_key

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cache_entries (
    cache_key             TEXT PRIMARY KEY,
    response_body         BLOB NOT NULL,
    response_status_code  INTEGER NOT NULL,
    original_wallclock_ms INTEGER NOT NULL,
    original_backend_name TEXT,
    recorded_at_ms        INTEGER NOT NULL,
    hit_count             INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_cache_entries_recorded_at
    ON cache_entries (recorded_at_ms);

INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '{SCHEMA_VERSION}');
"""


@dataclass
class CacheEntry:
    response_body: bytes
    response_status_code: int
    original_wallclock_ms: int
    original_backend_name: str | None
    hit_count: int


@dataclass
class CacheStats:
    """Live counters since process start. Reset only when the gateway
    process restarts; the on-row `hit_count` is the durable counterpart."""

    hits:    int = 0
    misses:  int = 0
    records: int = 0
    errors:  int = 0
    # Headline cumulative info from the DB itself, refreshed on demand.
    entry_count: int = 0
    db_size_bytes: int = 0
    db_path: str = ""
    schema_version: str = SCHEMA_VERSION

    @property
    def hit_rate(self) -> float | None:
        total = self.hits + self.misses
        return (self.hits / total) if total > 0 else None

    def to_dict(self) -> dict:
        d = {
            "hits": self.hits, "misses": self.misses,
            "records": self.records, "errors": self.errors,
            "hit_rate": self.hit_rate,
            "entry_count": self.entry_count,
            "db_size_bytes": self.db_size_bytes,
            "db_path": self.db_path,
            "schema_version": self.schema_version,
        }
        return d


class Cache:
    """Persistent KV cache keyed on the request hash from `keying.derive_key`.

    Open once at gateway startup; pass the instance into the request handler.
    The gateway will call:
      entry = cache.get(key)               # on every request
      cache.put(key, body, status, ms, backend)  # on every upstream success

    All counters are advanced inside these methods so the app handler is
    one line each.
    """

    def __init__(self, db_path: str | Path):
        self._path = str(Path(db_path).expanduser())
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._tls = threading.local()
        self._stats_lock = threading.Lock()
        self.stats = CacheStats(db_path=self._path)

        # Initialise schema on the calling thread. Subsequent connections
        # use _conn() to get a thread-local handle.
        conn = self._conn()
        conn.executescript(_SCHEMA)
        conn.commit()

        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        observed = row[0] if row else None
        if observed != SCHEMA_VERSION:
            # Be loud: a mismatch means a tooling change shipped that the
            # cache file pre-dates (or vice versa). Either re-record from
            # scratch or write a migration — never read a stale shape.
            raise RuntimeError(
                f"cache schema mismatch: file {self._path!r} reports {observed!r}, "
                f"code expects {SCHEMA_VERSION!r}. Re-record or migrate."
            )
        self._refresh_db_stats()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, isolation_level=None)
            # WAL = concurrent reads while a write is in flight. Not
            # strictly needed at single-loop gateway scale but cheap and
            # makes the file easier to inspect with the sqlite3 CLI mid-run.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            self._tls.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._tls, "conn", None)
        if conn is not None:
            conn.close()
            self._tls.conn = None

    # ── lookups ──────────────────────────────────────────────────────────

    def get(self, key: str) -> CacheEntry | None:
        """Return cached entry, or None on miss. Atomically increments
        hit_count + counters on hit."""
        try:
            row = self._conn().execute(
                "SELECT response_body, response_status_code, original_wallclock_ms, "
                "       original_backend_name, hit_count "
                "FROM cache_entries WHERE cache_key = ?",
                (key,),
            ).fetchone()
        except sqlite3.Error as exc:
            with self._stats_lock:
                self.stats.errors += 1
            logger.warning("cache.get(%s): %s", short_key(key), exc)
            return None
        if row is None:
            with self._stats_lock:
                self.stats.misses += 1
            return None
        body, status, wallclock_ms, backend_name, hit_count = row
        # Bump the durable counter. If the bump itself fails (read-only
        # mount, disk full), don't pretend the hit didn't happen — we
        # still serve the entry, but record the error.
        try:
            self._conn().execute(
                "UPDATE cache_entries SET hit_count = hit_count + 1 "
                "WHERE cache_key = ?",
                (key,),
            )
        except sqlite3.Error as exc:
            logger.warning("cache.get(%s) hit_count bump failed: %s", short_key(key), exc)
            with self._stats_lock:
                self.stats.errors += 1
        with self._stats_lock:
            self.stats.hits += 1
        return CacheEntry(
            response_body=body if isinstance(body, bytes) else bytes(body),
            response_status_code=int(status),
            original_wallclock_ms=int(wallclock_ms),
            original_backend_name=backend_name,
            hit_count=int(hit_count) + 1,
        )

    def put(
        self,
        key: str,
        response_body: bytes,
        response_status_code: int,
        original_wallclock_ms: int,
        original_backend_name: str | None,
    ) -> None:
        """Upsert. Updates `recorded_at_ms` so the most recent recording
        wins; hit_count is preserved on existing rows."""
        now_ms = int(time.time() * 1000)
        try:
            # INSERT OR REPLACE would zero hit_count. Use the explicit
            # UPSERT form so re-record runs don't throw away accumulated
            # hits from earlier sessions.
            self._conn().execute(
                "INSERT INTO cache_entries "
                "  (cache_key, response_body, response_status_code, "
                "   original_wallclock_ms, original_backend_name, recorded_at_ms, "
                "   hit_count) "
                "VALUES (?, ?, ?, ?, ?, ?, 0) "
                "ON CONFLICT (cache_key) DO UPDATE SET "
                "  response_body         = excluded.response_body, "
                "  response_status_code  = excluded.response_status_code, "
                "  original_wallclock_ms = excluded.original_wallclock_ms, "
                "  original_backend_name = excluded.original_backend_name, "
                "  recorded_at_ms        = excluded.recorded_at_ms",
                (
                    key, response_body, response_status_code,
                    original_wallclock_ms, original_backend_name, now_ms,
                ),
            )
        except sqlite3.Error as exc:
            with self._stats_lock:
                self.stats.errors += 1
            logger.warning("cache.put(%s): %s", short_key(key), exc)
            return
        with self._stats_lock:
            self.stats.records += 1

    # ── stats ────────────────────────────────────────────────────────────

    def _refresh_db_stats(self) -> None:
        try:
            row = self._conn().execute(
                "SELECT COUNT(*) FROM cache_entries"
            ).fetchone()
            entry_count = int(row[0]) if row else 0
        except sqlite3.Error:
            entry_count = 0
        try:
            db_size_bytes = Path(self._path).stat().st_size
        except OSError:
            db_size_bytes = 0
        with self._stats_lock:
            self.stats.entry_count = entry_count
            self.stats.db_size_bytes = db_size_bytes

    def snapshot_stats(self) -> CacheStats:
        """Return a fresh snapshot with DB-derived fields updated. Cheap
        enough to call from `/cache/stats` per request."""
        self._refresh_db_stats()
        with self._stats_lock:
            return CacheStats(**self.stats.to_dict_for_clone())

    def reset_session_counters(self) -> None:
        with self._stats_lock:
            self.stats.hits = 0
            self.stats.misses = 0
            self.stats.records = 0
            self.stats.errors = 0


# Tiny helper so snapshot_stats can hand back an immutable-ish view
# without exposing the lock-protected instance.
def _cache_stats_to_dict_for_clone(self: CacheStats) -> dict:
    return {
        "hits": self.hits, "misses": self.misses,
        "records": self.records, "errors": self.errors,
        "entry_count": self.entry_count,
        "db_size_bytes": self.db_size_bytes,
        "db_path": self.db_path,
        "schema_version": self.schema_version,
    }


CacheStats.to_dict_for_clone = _cache_stats_to_dict_for_clone  # type: ignore[attr-defined]
