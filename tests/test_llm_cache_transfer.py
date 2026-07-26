"""Tests for tools/llm_cache_transfer.py — the LLM-gateway cache export/import.

The load-bearing case is the WAL one: the gateway opens its cache with
`journal_mode=WAL`, so until sqlite checkpoints, entries live in `<db>-wal` and
NOT in `<db>`. Copying the `.db` file alone therefore ships an empty cache that
misses silently on every lookup (observed live: 4 KB .db next to an 800 KB -wal
holding all 45 entries). Exporting through a sqlite connection is what fixes it.
"""
from __future__ import annotations

import sqlite3

import pytest

from llm_gateway.cache import SCHEMA_VERSION, _SCHEMA
from tools import llm_cache_transfer as t


def _row(key: str, *, body: bytes = b'{"choices":[]}', backend: str = "deepseek_v4_pro",
         recorded_ms: int = 1_700_000_000_000, hit_count: int = 0) -> tuple:
    return (key, body, 200, 1234, backend, recorded_ms, hit_count)


def _make_cache(path, rows, *, wal: bool = False) -> sqlite3.Connection:
    """Build a cache file. With wal=True the connection is left OPEN in WAL mode
    and un-checkpointed, so the rows are still in the -wal — exactly the state a
    running gateway leaves behind."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    if wal:
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_SCHEMA)
    conn.executemany(
        f"INSERT OR REPLACE INTO cache_entries ({t._COLS}) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    if not wal:
        conn.close()
    return conn


# ── the WAL trap ──────────────────────────────────────────────────────────────

def test_export_reads_entries_still_sitting_in_the_wal(tmp_path):
    db = tmp_path / "live.db"
    conn = _make_cache(db, [_row("k1"), _row("k2")], wal=True)

    # Precondition: this is the trap. The main .db has the schema but the rows
    # are in the -wal, so a byte copy of the .db alone loses them.
    wal = tmp_path / "live.db-wal"
    assert wal.exists() and wal.stat().st_size > 0
    naive = tmp_path / "naive_copy.db"
    naive.write_bytes(db.read_bytes())
    cp = sqlite3.connect(str(naive))
    try:
        # Un-checkpointed, even the CREATE TABLE can still be in the -wal, so the
        # copy is either table-less or empty. Both are "the cache is gone".
        assert cp.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0] == 0
    except sqlite3.OperationalError as exc:
        assert "no such table" in str(exc)
    cp.close()

    # The tool reads through a connection, so it sees them.
    out = tmp_path / "export.db"
    assert t.export(db, out) == 2
    conn.close()

    got = sqlite3.connect(str(out))
    assert got.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0] == 2
    got.close()


def test_export_artifact_is_self_contained(tmp_path):
    db = tmp_path / "live.db"
    _make_cache(db, [_row("k1")])
    out = tmp_path / "export.db"
    t.export(db, out)

    # No sidecars to forget when scp'ing it.
    assert not (tmp_path / "export.db-wal").exists()
    conn = sqlite3.connect(str(out))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    conn.close()


def test_info_surfaces_uncheckpointed_wal_bytes(tmp_path):
    db = tmp_path / "live.db"
    conn = _make_cache(db, [_row("k1"), _row("k2")], wal=True)
    d = t.info(db)
    conn.close()
    assert d["entries"] == 2
    assert d["journal_mode"] == "wal"
    assert d["wal_file_bytes"] > 0          # the operator-visible warning signal


# ── export filters ────────────────────────────────────────────────────────────

def test_export_filters_by_backend_and_age(tmp_path):
    import time
    now_ms = int(time.time() * 1000)
    db = tmp_path / "c.db"
    _make_cache(db, [
        _row("old_ds", backend="deepseek_v4_pro", recorded_ms=now_ms - 10 * 86400_000),
        _row("new_ds", backend="deepseek_v4_pro", recorded_ms=now_ms),
        _row("new_qw", backend="qwen3", recorded_ms=now_ms),
    ])
    out = tmp_path / "e1.db"
    assert t.export(db, out, backend="deepseek_v4_pro") == 2
    out2 = tmp_path / "e2.db"
    assert t.export(db, out2, backend="deepseek_v4_pro", since_days=1) == 1


def test_export_refuses_to_clobber(tmp_path):
    db = tmp_path / "c.db"
    _make_cache(db, [_row("k1")])
    out = tmp_path / "e.db"
    t.export(db, out)
    with pytest.raises(t.TransferError, match="exists"):
        t.export(db, out)


# ── import / merge ────────────────────────────────────────────────────────────

def test_import_merges_and_keeps_local_by_default(tmp_path):
    local = tmp_path / "local.db"
    _make_cache(local, [_row("shared", body=b"LOCAL", hit_count=7), _row("only_local")])
    incoming = tmp_path / "in.db"
    _make_cache(incoming, [_row("shared", body=b"REMOTE"), _row("only_remote")])

    r = t.import_(local, incoming)
    assert r == {"incoming": 2, "new": 1, "already_present": 1, "applied": 2}

    conn = sqlite3.connect(str(local))
    assert conn.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0] == 3
    body, hits = conn.execute(
        "SELECT response_body, hit_count FROM cache_entries WHERE cache_key='shared'").fetchone()
    assert body == b"LOCAL" and hits == 7    # local row untouched
    conn.close()


def test_import_overwrite_prefers_incoming_but_keeps_local_hit_count(tmp_path):
    local = tmp_path / "local.db"
    _make_cache(local, [_row("shared", body=b"LOCAL", hit_count=7)])
    incoming = tmp_path / "in.db"
    _make_cache(incoming, [_row("shared", body=b"REMOTE", hit_count=1)])

    t.import_(local, incoming, overwrite=True)

    conn = sqlite3.connect(str(local))
    body, hits = conn.execute(
        "SELECT response_body, hit_count FROM cache_entries WHERE cache_key='shared'").fetchone()
    conn.close()
    # incoming body wins; hit_count is a LOCAL statistic and must not regress
    assert body == b"REMOTE" and hits == 7


def test_import_creates_target_when_absent(tmp_path):
    incoming = tmp_path / "in.db"
    _make_cache(incoming, [_row("k1"), _row("k2")])
    target = tmp_path / "nested" / "new.db"
    r = t.import_(target, incoming)
    assert r["new"] == 2 and target.exists()


def test_import_dry_run_changes_nothing(tmp_path):
    local = tmp_path / "local.db"
    _make_cache(local, [_row("shared")])
    incoming = tmp_path / "in.db"
    _make_cache(incoming, [_row("shared"), _row("fresh")])

    r = t.import_(local, incoming, dry_run=True)
    assert r == {"incoming": 2, "new": 1, "already_present": 1, "applied": 0}

    conn = sqlite3.connect(str(local))
    assert conn.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0] == 1
    conn.close()


# ── guards ────────────────────────────────────────────────────────────────────

def test_schema_version_mismatch_aborts(tmp_path):
    db = tmp_path / "c.db"
    _make_cache(db, [_row("k1")])
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    # A silently-accepted mismatch would produce a populated-looking cache that
    # misses on everything — abort instead.
    with pytest.raises(t.TransferError, match="schema_version"):
        t.info(db)


def test_non_cache_file_is_rejected(tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database at all")
    with pytest.raises(t.TransferError):
        t.info(junk)


def test_missing_source_is_rejected(tmp_path):
    with pytest.raises(t.TransferError, match="no cache"):
        t.info(tmp_path / "nope.db")


def test_schema_version_constant_is_the_gateways(tmp_path):
    # The tool must never carry its own copy of the schema.
    assert t.SCHEMA_VERSION == SCHEMA_VERSION


# ── cli ───────────────────────────────────────────────────────────────────────

def test_cli_roundtrip(tmp_path, capsys):
    db = tmp_path / "c.db"
    _make_cache(db, [_row("k1"), _row("k2")])
    out = tmp_path / "e.db"
    assert t.main(["export", "--db", str(db), "--out", str(out)]) == 0
    target = tmp_path / "t.db"
    assert t.main(["import", "--db", str(target), "--from", str(out)]) == 0
    assert "added 2 new" in capsys.readouterr().out


def test_cli_reports_error_without_traceback(tmp_path, capsys):
    assert t.main(["info", "--db", str(tmp_path / "nope.db")]) == 2
    assert "error:" in capsys.readouterr().err
