"""
Checkpointer factory for LangGraphAgent.

Why we want checkpoints (in one paragraph):
  Worker restarts (HealthMonitor expiry, OOM, deploy) currently re-run the
  graph from `precheck → fetch_trace`, which re-spends the LLM tokens in
  react_loop — the single biggest cost in any run. With a checkpointer
  attached, restart resumes at the next un-completed node. Side-effect
  *correctness* is still guaranteed by the provider's idempotency layer
  (deterministic branches, three-state push, MR lookup-then-create); the
  checkpointer is purely a performance / cost optimization.

Backend choice:
  - SQLite: default, works everywhere, single-file DB at
    `~/.sdlcma/checkpoints/<run_id>.sqlite` (overridable). Standalone mode
    uses this; the GitLab worker also defaults to it because each spawned
    worker process can have its own DB on the host.
  - Redis: opt-in via env var BF_CHECKPOINT_BACKEND=redis (uses the same
    Redis the orchestrator already needs). Picks up `redis_url` from the
    worker config. Useful when you want checkpoints to survive a host
    swap, or to inspect them centrally.
  - None: opt-out via BF_CHECKPOINT_BACKEND=none. The graph runs without
    checkpointing — mainly useful in tests and short standalone runs where
    resume is not interesting.

We intentionally do NOT default to MemorySaver: it's lost on process exit,
which defeats the entire point. If you want it, ask explicitly via
BF_CHECKPOINT_BACKEND=memory (used in unit tests).
"""

from __future__ import annotations
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _default_sqlite_path() -> Path:
    """Standard location: ~/.sdlcma/checkpoints/state.sqlite. Overridable
    via BF_CHECKPOINT_PATH."""
    override = os.environ.get("BF_CHECKPOINT_PATH")
    if override:
        return Path(override)
    home = Path(os.environ.get("HOME") or os.path.expanduser("~"))
    return home / ".sdlcma" / "checkpoints" / "state.sqlite"


def build_checkpointer(backend: str | None = None) -> Any | None:
    """Construct a LangGraph checkpointer per the requested backend.

    Returns None when backend is "none" or omitted (opt-out path). The
    LangGraphAgent treats None as "compile graph without a checkpointer",
    preserving the pre-checkpointing behavior exactly.

    Backend selection:
      explicit arg > BF_CHECKPOINT_BACKEND env var > "sqlite" default
    """
    backend = backend or os.environ.get("BF_CHECKPOINT_BACKEND") or "sqlite"
    backend = backend.lower()

    if backend == "none":
        logger.info("checkpointer disabled (backend=none)")
        return None

    if backend == "memory":
        # In-process only — useful for tests and one-shot runs.
        from langgraph.checkpoint.memory import MemorySaver
        logger.info("checkpointer: MemorySaver (in-process, no persistence)")
        return MemorySaver()

    if backend == "sqlite":
        # File-backed; survives process restart on the same host.
        from langgraph.checkpoint.sqlite import SqliteSaver
        import sqlite3

        db_path = _default_sqlite_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # `check_same_thread=False` lets the LangGraph internals share the
        # connection across the heartbeat thread + main thread without
        # spawning multiple connections on the same SQLite file.
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        logger.info("checkpointer: SqliteSaver db=%s", db_path)
        return SqliteSaver(conn)

    if backend == "redis":
        # Lazy import — Redis checkpointer is an extra dep.
        try:
            from langgraph.checkpoint.redis import RedisSaver
        except ImportError as exc:
            raise RuntimeError(
                "BF_CHECKPOINT_BACKEND=redis requested but "
                "langgraph-checkpoint-redis is not installed. "
                "Install it or set BF_CHECKPOINT_BACKEND=sqlite."
            ) from exc
        from settings import worker_cfg as cfg
        url = os.environ.get("BF_CHECKPOINT_REDIS_URL") or cfg.redis_url
        logger.info("checkpointer: RedisSaver url=%s", url)
        return RedisSaver.from_url(url)

    raise ValueError(
        f"unknown BF_CHECKPOINT_BACKEND={backend!r} "
        f"(expected one of: sqlite, redis, memory, none)"
    )
