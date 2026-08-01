"""U1 / U7 — the step checkpoint store (plan item W2).

Covers the storage layer on its own: round-trip, atomicity, TTL, per-run
isolation (12-15 instances run concurrently on ls4900, so records must not be
able to see each other), and the fail-fast/forgiving split — an unusable
*configuration* raises at startup, an unreadable *record* only degrades.

See /mnt/d/PL/sdlcma/W2-step-checkpoint-design.md §6, §8.1.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "bf_worker"))

from services.step_checkpoint import (  # noqa: E402
    SCHEMA,
    FileStore,
    NoopStore,
    StepCheckpointError,
    build_step_checkpoint_store,
    run_fingerprint,
)


@pytest.fixture
def store(tmp_path) -> FileStore:
    return FileStore(tmp_path / "cp")


# ── round-trip ───────────────────────────────────────────────────────────────

def test_save_load_round_trip(store):
    store.save("BUG-1", "agent-0", {"messages": [{"role": "user", "content": "hi"}], "step": 3})
    rec = store.load("BUG-1", "agent-0")
    assert rec["step"] == 3
    assert rec["messages"] == [{"role": "user", "content": "hi"}]
    assert rec["schema"] == SCHEMA
    assert rec["updated_at"] > 0


def test_load_missing_returns_none(store):
    assert store.load("BUG-1", "agent-0") is None


def test_documents_within_a_run_are_independent(store):
    store.save("BUG-1", "env", {"container_id": "abc"})
    store.save("BUG-1", "agent-0", {"step": 1})
    assert store.load("BUG-1", "env")["container_id"] == "abc"
    assert store.load("BUG-1", "agent-0")["step"] == 1


# ── isolation + purge ────────────────────────────────────────────────────────

def test_runs_are_isolated_and_purge_only_touches_one(store):
    """The property that makes 12-15 concurrent instances safe."""
    for run in ("BUG-A", "BUG-B", "BUG-C"):
        store.save(run, "env", {"container_id": run})
        store.save(run, "agent-0", {"step": 1})

    store.purge_run("BUG-B")

    assert store.load("BUG-B", "env") is None
    assert store.load("BUG-B", "agent-0") is None
    assert store.load("BUG-A", "env")["container_id"] == "BUG-A"
    assert store.load("BUG-C", "agent-0")["step"] == 1


def test_purge_of_unknown_run_is_a_noop(store):
    store.purge_run("never-existed")   # must not raise


def test_distinct_keys_never_share_a_directory(store):
    """Sanitising must not be able to collapse two different runs into one."""
    a, b = "bug/1", "bug:1"            # both sanitise to "bug_1"
    store.save(a, "env", {"container_id": "A"})
    store.save(b, "env", {"container_id": "B"})
    assert store.load(a, "env")["container_id"] == "A"
    assert store.load(b, "env")["container_id"] == "B"


# ── atomicity ────────────────────────────────────────────────────────────────

def test_write_is_atomic_and_leaves_no_temp_file(store):
    store.save("BUG-1", "agent-0", {"step": 1})
    store.save("BUG-1", "agent-0", {"step": 2})
    files = sorted(p.name for p in (store.root / "BUG-1").iterdir())
    assert files == ["agent-0.json"], f"stray temp file left behind: {files}"
    assert store.load("BUG-1", "agent-0")["step"] == 2


# ── runtime forgiveness ──────────────────────────────────────────────────────

def test_corrupt_record_is_ignored_not_raised(store):
    store.save("BUG-1", "agent-0", {"step": 1})
    (store.root / "BUG-1" / "agent-0.json").write_text("{not json", encoding="utf-8")
    assert store.load("BUG-1", "agent-0") is None


def test_foreign_schema_is_ignored(store):
    path = store.root / "BUG-1"
    path.mkdir(parents=True)
    (path / "agent-0.json").write_text(
        json.dumps({"schema": SCHEMA + 99, "step": 1, "updated_at": time.time()}), encoding="utf-8")
    assert store.load("BUG-1", "agent-0") is None


def test_expired_record_is_dropped_and_run_purged(tmp_path):
    store = FileStore(tmp_path / "cp", ttl_s=1)
    store.save("BUG-1", "env", {"container_id": "abc"})
    store.save("BUG-1", "agent-0", {"step": 1})

    stale = json.loads((store.root / "BUG-1" / "agent-0.json").read_text())
    stale["updated_at"] = time.time() - 3600
    (store.root / "BUG-1" / "agent-0.json").write_text(json.dumps(stale), encoding="utf-8")

    assert store.load("BUG-1", "agent-0") is None
    # A record this old means the container is long gone (mini's own 2h
    # container_timeout), so the whole run goes, not just the one document.
    assert store.load("BUG-1", "env") is None


def test_ttl_zero_disables_expiry(tmp_path):
    store = FileStore(tmp_path / "cp", ttl_s=0)
    store.save("BUG-1", "agent-0", {"step": 1})
    stale = json.loads((store.root / "BUG-1" / "agent-0.json").read_text())
    stale["updated_at"] = time.time() - 10 ** 6
    (store.root / "BUG-1" / "agent-0.json").write_text(json.dumps(stale), encoding="utf-8")
    assert store.load("BUG-1", "agent-0")["step"] == 1


# ── fingerprint ──────────────────────────────────────────────────────────────

def test_fingerprint_is_stable_and_order_independent():
    assert run_fingerprint(a=1, b="x") == run_fingerprint(b="x", a=1)


def test_fingerprint_changes_with_any_part():
    base = run_fingerprint(instance_id="astropy-1", workflow_mode=0, task="t")
    assert base != run_fingerprint(instance_id="astropy-2", workflow_mode=0, task="t")
    assert base != run_fingerprint(instance_id="astropy-1", workflow_mode=1, task="t")
    assert base != run_fingerprint(instance_id="astropy-1", workflow_mode=0, task="t2")


def test_fingerprint_survives_unserialisable_parts():
    """agent config carries Paths; hashing must not explode on them."""
    assert run_fingerprint(output_path=Path("/tmp/x")) == run_fingerprint(output_path=Path("/tmp/x"))


# ── factory ──────────────────────────────────────────────────────────────────

def test_default_backend_is_noop(monkeypatch):
    monkeypatch.delenv("BF_STEP_CHECKPOINT", raising=False)
    store = build_step_checkpoint_store()
    assert isinstance(store, NoopStore)
    assert store.enabled is False
    # Inert: writing then reading gives nothing back, so every call site can
    # stay free of `if store is not None`.
    store.save("BUG-1", "agent-0", {"step": 1})
    assert store.load("BUG-1", "agent-0") is None


def test_file_backend_selected_by_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BF_STEP_CHECKPOINT", "file")
    monkeypatch.setenv("BF_STEP_CHECKPOINT_DIR", str(tmp_path / "cp"))
    store = build_step_checkpoint_store()
    assert isinstance(store, FileStore) and store.enabled is True


def test_explicit_arg_beats_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BF_STEP_CHECKPOINT", "file")
    monkeypatch.setenv("BF_STEP_CHECKPOINT_DIR", str(tmp_path / "cp"))
    assert isinstance(build_step_checkpoint_store("none"), NoopStore)


def test_redis_backend_raises_rather_than_falling_back(monkeypatch):
    """Deferred to W4 — and silence would be worse than an error."""
    monkeypatch.setenv("BF_STEP_CHECKPOINT", "redis")
    with pytest.raises(StepCheckpointError, match="W4"):
        build_step_checkpoint_store()


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("BF_STEP_CHECKPOINT", "sqlite")
    with pytest.raises(StepCheckpointError, match="unknown"):
        build_step_checkpoint_store()


def test_unwritable_dir_raises_at_startup(monkeypatch, tmp_path):
    """Startup-strict: better than discovering mid-sweep that nothing saved."""
    blocked = tmp_path / "ro"
    blocked.mkdir()
    blocked.chmod(0o500)
    monkeypatch.setenv("BF_STEP_CHECKPOINT", "file")
    monkeypatch.setenv("BF_STEP_CHECKPOINT_DIR", str(blocked / "cp"))
    try:
        if os.access(blocked, os.W_OK):        # running as root — chmod is advisory
            pytest.skip("cannot make a directory unwritable as this user")
        with pytest.raises(StepCheckpointError, match="not writable"):
            build_step_checkpoint_store()
    finally:
        blocked.chmod(0o700)


def test_bad_ttl_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("BF_STEP_CHECKPOINT", "file")
    monkeypatch.setenv("BF_STEP_CHECKPOINT_DIR", str(tmp_path / "cp"))
    monkeypatch.setenv("BF_STEP_CHECKPOINT_TTL_S", "soon")
    with pytest.raises(StepCheckpointError, match="TTL"):
        build_step_checkpoint_store()


def test_the_writability_probe_is_not_shared_state(tmp_path, monkeypatch):
    """Concurrent workers must not fight over one probe file.

    Real incident (2026-08-01, L2c-w3 chaos arm): every worker probed the same
    `.writable` path, so two of them interleaved as write/write/unlink/unlink and
    the second unlink raised FileNotFoundError — which this function's OSError
    handler turns into a fatal startup error. Two runs died before their first
    LLM call. A probe that only proves a directory is writable has no business
    being a rendezvous point.
    """
    import threading

    monkeypatch.setenv("BF_STEP_CHECKPOINT", "file")
    monkeypatch.setenv("BF_STEP_CHECKPOINT_DIR", str(tmp_path / "cp"))
    errors: list[BaseException] = []

    def build():
        try:
            for _ in range(25):
                build_step_checkpoint_store()
        except BaseException as exc:      # noqa: BLE001 — the point is to see it
            errors.append(exc)

    threads = [threading.Thread(target=build) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent construction raised: {errors[:2]}"
    leftovers = list((tmp_path / "cp").glob(".writable*"))
    assert leftovers == [], f"probe files were left behind: {leftovers}"
