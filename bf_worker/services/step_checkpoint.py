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

    @abstractmethod
    def names(self, run_key: str) -> list[str]:
        """Document names present for this run, unordered.

        Needed to answer "does this run still have unfinished work?" without
        knowing how many agents it has (the staged modes have several). The
        caller loads each one it cares about.
        """


class NoopStore(StepCheckpointStore):
    """The default. Keeps every call site free of `if store is not None`."""

    enabled = False

    def load(self, run_key: str, name: str) -> dict | None:
        return None

    def save(self, run_key: str, name: str, doc: dict) -> None:
        return None

    def purge_run(self, run_key: str) -> None:
        return None

    def names(self, run_key: str) -> list[str]:
        return []


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

    def names(self, run_key: str) -> list[str]:
        try:
            # `.json.tmp` files are mid-write states, not documents.
            return sorted(p.stem for p in self._dir(run_key).glob("*.json"))
        except OSError:
            return []


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
            # Unique per probe, and forgiving on removal. A shared ".writable"
            # is shared *state*: with 15 instances in flight — and more when
            # chaos restarts workers — two of them interleave as write/write/
            # unlink/unlink, and the second unlink raises FileNotFoundError,
            # which this OSError handler turns into a fatal startup error. Two
            # runs died that way on ls4900 before the first LLM call. A probe
            # that only proves the directory is writable has no business being
            # a rendezvous point.
            probe = root / f".writable.{os.getpid()}.{os.urandom(4).hex()}"
            probe.write_text("", encoding="utf-8")
            probe.unlink(missing_ok=True)
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
            "BF_STEP_CHECKPOINT=redis will not be implemented (decided in plan item "
            "W4). A redis-backed record is readable from any node, but the eval "
            "container it points at is not: it lives on one node's dockerd. So the "
            "backend would only make a replacement worker on another node believe it "
            "can resume, find no container, and purge — an ability that does not "
            "exist, plus more traffic through the hardest branch to get right. On k8s "
            "the answer is 'file' on a node-local hostPath (K8S_WORKER_STEP_CHECKPOINT"
            "_HOST_PATH) plus soft node affinity on restart, so 'record readable' and "
            "'container attachable' stay true together. Use 'file'."
        )

    raise StepCheckpointError(
        f"unknown BF_STEP_CHECKPOINT={backend!r} (expected one of: none, file)"
    )


def purge_run_records(run_key: str) -> None:
    """Drop a run's records once the run has finished in-process (W2.5).

    Module-level rather than a method on the agent, because the caller that
    knows a run is *over* usually no longer holds the agent: in ver99 the
    `MiniSweAgent` is built inside the graph node and discarded when `fix()`
    returns, so `agent.finish_run()` is unreachable from the worker's exit
    path. The run key is all that is needed.

    Deliberately total: it runs in `finally` blocks, where raising would
    replace the real reason the process is exiting.

    Keeping records until here is what lets a worker that dies AFTER the mini
    loop (git apply / push / CI wait — tens of minutes in ver99) replay the
    finished loop for free instead of re-spending 15-40 LLM calls. The
    invariant that makes that safe is "a record exists ⇒ the previous process
    did not finish normally", which is exactly what calling this on every
    normal exit maintains.
    """
    if not run_key:
        return
    try:
        store = build_step_checkpoint_store()
        if store.enabled:
            store.purge_run(run_key)
    except Exception:
        logger.warning("failed to purge step checkpoint records for %s", run_key, exc_info=True)
