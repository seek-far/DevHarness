#!/usr/bin/env python3
"""
tools/llm_cache_transfer.py — export / import / inspect an LLM-gateway response cache.

The gateway's cache (`llm_gateway/cache.py`) is one sqlite file, and shipping it
between machines is the documented deployment model: record on box A (costs
money), replay on box B (costs nothing). Two things make a plain `cp`/`scp` of
that file WRONG, and this tool exists for both:

1. **The cache runs in WAL mode.** A live cache is `<db>` + `<db>-wal` + `<db>-shm`,
   and until a checkpoint happens the rows live in the `-wal`, not in `<db>`.
   Observed on ls4900 after a 45-call run: `<db>` = 4 KB (empty schema),
   `<db>-wal` = 800 KB (all 45 entries). `scp <db>` there ships an EMPTY cache
   that silently misses on every lookup — it does not error, it just costs you
   the whole bill again. Exporting through a sqlite connection reads THROUGH the
   WAL, and the artifact this writes is a single self-contained file
   (journal_mode=delete) with no sidecars to forget.

2. **Import must MERGE, not replace.** The target box usually has its own
   recorded entries; overwriting the file throws them away. `import` upserts per
   `cache_key`, keeping local rows by default (`--overwrite` to prefer incoming).

Subcommands:

    info    --db <path>                     entry count, size, WAL state, backends, age
    export  --db <path> --out <file>        [--backend NAME] [--since-days N]
    import  --db <path> --from <file>       [--overwrite] [--dry-run]

`export`/`import` are safe against a RUNNING gateway: the source is opened
read-only, and sqlite's own locking covers the writer. Nothing here mutates the
source cache.

Examples:
    # what have I actually got?
    python -m tools.llm_cache_transfer info --db /tmp/sdlcma_llm_cache.db

    # ship a box's cache somewhere else
    python -m tools.llm_cache_transfer export --db /tmp/sdlcma_llm_cache.db \
        --out /tmp/cache_export.db
    scp /tmp/cache_export.db other-host:/tmp/
    ssh other-host 'python -m tools.llm_cache_transfer import \
        --db /var/lib/sdlcma/llm_cache.db --from /tmp/cache_export.db'

⚠️ Put the live `db_path` somewhere PERSISTENT. A cache under `/tmp` is deleted
by the next reboot — that is how the minus box lost a 100-instance recording.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

# The gateway owns the schema; import it so this tool can never drift from it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llm_gateway.cache import SCHEMA_VERSION, _SCHEMA  # noqa: E402

_COLS = (
    "cache_key, response_body, response_status_code, original_wallclock_ms, "
    "original_backend_name, recorded_at_ms, hit_count"
)
_BATCH = 200


class TransferError(RuntimeError):
    pass


# ── connections ───────────────────────────────────────────────────────────────

def open_source(path: str | Path) -> sqlite3.Connection:
    """Read-only connection. Reading through a connection (rather than copying
    the file) is the whole point — it sees rows still sitting in the -wal."""
    p = Path(path)
    if not p.exists():
        raise TransferError(f"no cache at {p}")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    _check_schema(conn, str(p))
    return conn


def open_target(path: str | Path, *, create: bool) -> sqlite3.Connection:
    p = Path(path)
    if not p.exists():
        if not create:
            raise TransferError(f"no cache at {p}")
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), isolation_level=None)
    conn.executescript(_SCHEMA)          # idempotent (CREATE TABLE IF NOT EXISTS)
    _check_schema(conn, str(p))
    return conn


def _check_schema(conn: sqlite3.Connection, label: str) -> None:
    """A schema mismatch must abort loudly. Importing entries keyed under a
    different keying scheme would produce a cache that looks populated and
    misses on everything — the worst failure mode there is."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.DatabaseError as exc:
        raise TransferError(f"{label}: not an LLM-gateway cache ({exc})") from exc
    got = row[0] if row else None
    if got != SCHEMA_VERSION:
        raise TransferError(
            f"{label}: schema_version {got!r} != this build's {SCHEMA_VERSION!r} — refusing")


# ── info ──────────────────────────────────────────────────────────────────────

def info(db: str | Path) -> dict:
    p = Path(db)
    conn = open_source(p)
    n = conn.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0]
    hits = conn.execute("SELECT COALESCE(SUM(hit_count), 0) FROM cache_entries").fetchone()[0]
    lo, hi = conn.execute(
        "SELECT MIN(recorded_at_ms), MAX(recorded_at_ms) FROM cache_entries").fetchone()
    backends = conn.execute(
        "SELECT COALESCE(original_backend_name, '(none)'), COUNT(*) "
        "FROM cache_entries GROUP BY 1 ORDER BY 2 DESC").fetchall()
    body_bytes = conn.execute(
        "SELECT COALESCE(SUM(LENGTH(response_body)), 0) FROM cache_entries").fetchone()[0]
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    wal = p.with_name(p.name + "-wal")
    return {
        "path": str(p),
        "entries": n,
        "cumulative_hits": hits,
        "response_bytes": body_bytes,
        "db_file_bytes": p.stat().st_size,
        "wal_file_bytes": wal.stat().st_size if wal.exists() else 0,
        "journal_mode": mode,
        "recorded_from_ms": lo,
        "recorded_to_ms": hi,
        "backends": backends,
    }


def _fmt_ms(ms: int | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000)) if ms else "-"


def print_info(d: dict) -> None:
    print(f"cache: {d['path']}")
    print(f"  entries          {d['entries']}")
    print(f"  cumulative hits  {d['cumulative_hits']}")
    print(f"  response bodies  {d['response_bytes'] / 1024:.1f} KiB")
    print(f"  db file          {d['db_file_bytes'] / 1024:.1f} KiB "
          f"(journal_mode={d['journal_mode']})")
    if d["wal_file_bytes"]:
        print(f"  -wal file        {d['wal_file_bytes'] / 1024:.1f} KiB "
              f"← NOT in the .db yet; `cp` of the .db alone would lose this")
    print(f"  recorded         {_fmt_ms(d['recorded_from_ms'])} .. {_fmt_ms(d['recorded_to_ms'])}")
    for name, cnt in d["backends"]:
        print(f"    {name:<28} {cnt}")


# ── export ────────────────────────────────────────────────────────────────────

def export(db: str | Path, out: str | Path, *,
           backend: str | None = None, since_days: float | None = None) -> int:
    src = open_source(db)
    outp = Path(out)
    if outp.exists():
        raise TransferError(f"{outp} exists — refusing to overwrite an export")
    dst = open_target(outp, create=True)
    # Single self-contained artifact: no -wal/-shm sidecars for the operator to
    # forget when they scp it.
    dst.execute("PRAGMA journal_mode = DELETE")

    where, params = [], []
    if backend:
        where.append("original_backend_name = ?")
        params.append(backend)
    if since_days is not None:
        where.append("recorded_at_ms >= ?")
        params.append(int((time.time() - since_days * 86400) * 1000))
    sql = f"SELECT {_COLS} FROM cache_entries"
    if where:
        sql += " WHERE " + " AND ".join(where)

    cur = src.execute(sql, params)
    n = 0
    while True:
        rows = cur.fetchmany(_BATCH)
        if not rows:
            break
        dst.executemany(
            f"INSERT OR REPLACE INTO cache_entries ({_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        n += len(rows)
    dst.commit()
    dst.close()
    src.close()
    return n


# ── import ────────────────────────────────────────────────────────────────────

def import_(db: str | Path, src_file: str | Path, *,
            overwrite: bool = False, dry_run: bool = False) -> dict:
    src = open_source(src_file)
    incoming = src.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0]

    if dry_run:
        # Report the overlap without touching the target.
        dst = open_source(db)
        have = {k for (k,) in dst.execute("SELECT cache_key FROM cache_entries")}
        keys = {k for (k,) in src.execute("SELECT cache_key FROM cache_entries")}
        dst.close()
        src.close()
        return {"incoming": incoming, "new": len(keys - have),
                "already_present": len(keys & have), "applied": 0}

    dst = open_target(db, create=True)
    before = dst.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0]
    if overwrite:
        # Incoming wins, but hit_count is a LOCAL statistic — never let a remote
        # export reset this box's usage counters.
        stmt = (f"INSERT INTO cache_entries ({_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET "
                "  response_body = excluded.response_body, "
                "  response_status_code = excluded.response_status_code, "
                "  original_wallclock_ms = excluded.original_wallclock_ms, "
                "  original_backend_name = excluded.original_backend_name, "
                "  recorded_at_ms = excluded.recorded_at_ms, "
                "  hit_count = MAX(cache_entries.hit_count, excluded.hit_count)")
    else:
        stmt = (f"INSERT INTO cache_entries ({_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(cache_key) DO NOTHING")

    cur = src.execute(f"SELECT {_COLS} FROM cache_entries")
    while True:
        rows = cur.fetchmany(_BATCH)
        if not rows:
            break
        dst.executemany(stmt, rows)
    dst.commit()
    after = dst.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0]
    dst.close()
    src.close()
    return {"incoming": incoming, "new": after - before,
            "already_present": incoming - (after - before), "applied": incoming}


# ── cli ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("info", help="inspect a cache (incl. un-checkpointed WAL bytes)")
    p.add_argument("--db", required=True)

    p = sub.add_parser("export", help="write a single portable file (WAL-safe)")
    p.add_argument("--db", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--backend", help="only entries recorded by this backend")
    p.add_argument("--since-days", type=float, help="only entries newer than N days")

    p = sub.add_parser("import", help="merge an export into a cache (keeps local rows)")
    p.add_argument("--db", required=True)
    p.add_argument("--from", dest="src", required=True)
    p.add_argument("--overwrite", action="store_true",
                   help="incoming entries win on cache_key collision")
    p.add_argument("--dry-run", action="store_true", help="report overlap, change nothing")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "info":
            print_info(info(args.db))
        elif args.cmd == "export":
            n = export(args.db, args.out, backend=args.backend, since_days=args.since_days)
            print(f"exported {n} entr{'y' if n == 1 else 'ies'} → {args.out}")
            print_info(info(args.out))
        elif args.cmd == "import":
            r = import_(args.db, args.src, overwrite=args.overwrite, dry_run=args.dry_run)
            verb = "would add" if args.dry_run else "added"
            print(f"{args.src}: {r['incoming']} incoming, {verb} {r['new']} new, "
                  f"{r['already_present']} already present → {args.db}")
    except TransferError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
