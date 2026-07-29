"""
Intra-loop step checkpoint store (plan item W2).

Why this exists, in one paragraph:
  A ver99 SWE-bench run spends 95% of its cost inside ONE LangGraph node —
  mini's ReAct loop, 15-40 LLM calls deep. LangGraph's checkpointer resumes at
  *node boundaries*, so a worker killed mid-loop re-runs that whole node and
  re-spends the whole trajectory. This store persists the loop's state at every
  iteration so a restart can continue from the last completed step instead.
  It is deliberately independent of LangGraph: the two stack (see
  docs/architecture.md), and this one works even when node checkpointing is off.

What makes resume possible at all is that mini's docker environment starts a
detached *sibling* container (`docker run -d … sleep 2h`) whose cleanup hangs off
`__del__` — a SIGKILLed worker never runs it, so the container with all the
already-edited files is still alive when the replacement worker starts. This
store keeps the pointer to it.

Backends:
  - none (DEFAULT): NoopStore. Every call is a no-op and `load` returns None, so
    the whole feature is inert — the pre-W2 code path, byte for byte.
  - file: one directory per run under BF_STEP_CHECKPOINT_DIR. Atomic writes
    (tmp + os.replace).
  - redis: NOT implemented yet, and it raises rather than falling back. It is
    deferred to plan item W4 (k8s), where it must land together with node
    affinity — the eval container is node-local, so a redis-backed record that
    a pod on ANOTHER node can read would be an ability that does not exist.

Strict at startup, forgiving at runtime: an unusable configuration raises
immediately (a half-enabled resume is the worst thing to debug), but a failure
while the loop is running only degrades to "did not save / will not resume" and
never interrupts the agent.

See /mnt/d/PL/sdlcma/W2-step-checkpoint-design.md §6.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA = 1

_DEFAULT_TTL_S = 14400          # 4h — deliberately > mini's 2h container_timeout
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_WARN_BYTES = 32 * 1024 * 1024  # a record this big means something is wrong


class StepCheckpointError(RuntimeError):
    """Configuration is unusable — raised at startup, never mid-loop."""


def run_fingerprint(**parts) -> str:
    """Identity of "the thing we are about to run".

    A record may only be spliced into a run whose fingerprint matches. This is
    the first gate against reusing a key: the batch path uses
    `bug_id == instance_id`, which repeats across sweeps by construction.
    """
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class StepCheckpointStore(ABC):
    """KV of small JSON documents, namespaced by run.

    `name` is a document within the run: "env" for the container pointer,
    "agent-<n>" for each agent's loop state (n > 0 only in the staged workflow
    modes, where several agents share one environment).
    """

    enabled: bool = False

    @abstractmethod
    def load(self, run_key: str, name: str) -> dict | None: ...

    @abstractmethod
    def save(self, run_key: str, name: str, doc: dict) -> None: ...

    @abstractmethod
    def purge_run(self, run_key: str) -> None: ...


class NoopStore(StepCheckpointStore):
    """The default. Keeps every call site free of `if store is not None`."""

    enabled = False

    def load(self, run_key: str, name: str) -> dict | None:
        return None

    def save(self, run_key: str, name: str, doc: dict) -> None:
        return None

    def purge_run(self, run_key: str) -> None:
        return None


class FileStore(StepCheckpointStore):
    """One directory per run:  <root>/<run_key>/{env,agent-0,agent-1}.json

    A directory per run (rather than flat files) is what makes `purge_run` a
    single rmtree that cannot possibly touch another run's records — which
    matters because ls4900 runs 12-15 instances concurrently.
    """

    enabled = True

    def __init__(self, root: Path, ttl_s: int = _DEFAULT_TTL_S):
        self.root = Path(root)
        self.ttl_s = int(ttl_s)

    # ── paths ────────────────────────────────────────────────────────────────

    @staticmethod
    def _safe(run_key: str) -> str:
        """Filesystem-safe directory name that stays 1:1 with the run key.

        bug_ids and instance_ids are already safe; the hash suffix is only
        appended when sanitising actually changed something, so two different
        keys can never collapse onto the same directory.
        """
        safe = _UNSAFE.sub("_", run_key)[:120]
        if safe != run_key:
            safe = f"{safe}-{hashlib.sha256(run_key.encode()).hexdigest()[:8]}"
        return safe or "unnamed"

    def _dir(self, run_key: str) -> Path:
        return self.root / self._safe(run_key)

    def _path(self, run_key: str, name: str) -> Path:
        return self._dir(run_key) / f"{_UNSAFE.sub('_', name)}.json"

    # ── api ──────────────────────────────────────────────────────────────────

    def load(self, run_key: str, name: str) -> dict | None:
        path = self._path(run_key, name)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception as exc:  # corrupt / truncated / unreadable
            logger.warning("step checkpoint unreadable at %s (%s) — ignoring", path, exc)
            return None
        if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
            logger.warning("step checkpoint at %s has schema %r — ignoring", path, doc.get("schema"))
            return None
        age = time.time() - float(doc.get("updated_at") or 0)
        if self.ttl_s > 0 and age > self.ttl_s:
            # Belt to the container-liveness braces: by now mini's own 2h
            # container_timeout has long since removed the container anyway.
            logger.info("step checkpoint at %s is %.0fs old (> ttl %ds) — purging run",
                        path, age, self.ttl_s)
            self.purge_run(run_key)
            return None
        return doc

    def save(self, run_key: str, name: str, doc: dict) -> None:
        path = self._path(run_key, name)
        payload = dict(doc)
        payload["schema"] = SCHEMA
        payload["updated_at"] = time.time()
        blob = json.dumps(payload, ensure_ascii=False)
        if len(blob) > _WARN_BYTES:
            # Never truncate: a truncated record resumes into a wrong state,
            # which is far worse than not resuming at all.
            logger.warning("step checkpoint %s/%s is %.1f MB — not truncating, but "
                           "this is far above the expected few hundred KB",
                           run_key, name, len(blob) / 1e6)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(blob, encoding="utf-8")
        os.replace(tmp, path)   # atomic within the same directory

    def purge_run(self, run_key: str) -> None:
        shutil.rmtree(self._dir(run_key), ignore_errors=True)


def _default_dir() -> Path:
    override = os.environ.get("BF_STEP_CHECKPOINT_DIR")
    if override:
        return Path(override)
    home = Path(os.environ.get("HOME") or os.path.expanduser("~"))
    return home / ".sdlcma" / "step_checkpoints"


def build_step_checkpoint_store(backend: str | None = None) -> StepCheckpointStore:
    """Construct the store per the requested backend.

    Selection: explicit arg > BF_STEP_CHECKPOINT env var > "none".
    Mirrors services/checkpointer.py on purpose — same precedence, same
    fail-fast on an unknown value — so there is only one mental model here.
    """
    backend = (backend or os.environ.get("BF_STEP_CHECKPOINT") or "none").strip().lower()

    if backend == "none":
        return NoopStore()

    if backend == "file":
        try:
            ttl = int(os.environ.get("BF_STEP_CHECKPOINT_TTL_S") or _DEFAULT_TTL_S)
        except ValueError as exc:
            raise StepCheckpointError(
                f"BF_STEP_CHECKPOINT_TTL_S={os.environ.get('BF_STEP_CHECKPOINT_TTL_S')!r} "
                f"is not an integer"
            ) from exc
        root = _default_dir()
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".writable"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            # Startup-strict: refusing here beats discovering half way through a
            # 100-instance sweep that nothing was ever saved.
            raise StepCheckpointError(
                f"BF_STEP_CHECKPOINT=file but {root} is not writable ({exc})"
            ) from exc
        logger.info("step checkpoint: FileStore dir=%s ttl=%ds", root, ttl)
        return FileStore(root, ttl)

    if backend == "redis":
        raise StepCheckpointError(
            "BF_STEP_CHECKPOINT=redis is not implemented yet (deferred to plan item W4, "
            "where it has to land together with node affinity — the eval container is "
            "node-local, so a record readable from another node would promise a "
            "recovery that cannot happen). Use 'file'."
        )

    raise StepCheckpointError(
        f"unknown BF_STEP_CHECKPOINT={backend!r} (expected one of: none, file)"
    )
