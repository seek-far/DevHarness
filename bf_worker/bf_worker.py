"""
worker.py — BugFix Worker entry point.

Responsibilities:
  - Parse CLI args
  - Maintain Redis heartbeat
  - Build and invoke the LangGraph
  - Handle SIGINT / SIGTERM gracefully

Usage: python worker.py --bug-id BUG-123
"""

from __future__ import annotations
import argparse
import asyncio
import logging
import os
import shutil
import signal
import stat
import sys
import time
from pathlib import Path

# Repo root must be on sys.path BEFORE the cascading imports below — line 28's
# `from agent_config import ...` reaches into graph/routing.py which needs
# `from settings import worker_cfg`. Use __file__ (not Path.cwd()) so the
# worker doesn't depend on the spawner's cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

from agents.base import BugInput
from agent_config import load_agent_spec, make_agent, maybe_reexec_for_agent_ref
from providers.gitlab_provider import GitLabProvider
from services.llm_model_check import check_or_abort as _check_llm_model
from services.step_checkpoint import purge_run_records

from settings import worker_cfg as cfg

def _rm_readonly(func, path, exc_info):
    """Error handler for shutil.rmtree: clear read-only flag and retry."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


# ── heartbeat ──────────────────────────────────────────────────────────────────

async def _heartbeat_loop(r: aioredis.Redis, hb_key: str) -> None:
    # Heartbeat VALUE is a unix-ms timestamp (string-encoded). HealthMonitor
    # only checks TTL liveness (value-format-agnostic), so any consumer that
    # also wants "when did the worker last refresh" (load_sampler, phase-2
    # latency reporter) can GET the value and parse — no separate channel
    # needed. First write fires immediately (loop body runs before sleep),
    # so the FIRST timestamp written is also the worker-ready marker for
    # phase-2 latency.
    while True:
        ts_ms = str(time.time_ns() // 1_000_000).encode()
        await r.setex(hb_key, cfg.worker_heartbeat_ttl * 2, ts_ms)
        logger.debug("heartbeat refreshed key=%s ttl=%ds ts_ms=%s",
                     hb_key, cfg.worker_heartbeat_ttl, ts_ms.decode())
        await asyncio.sleep(cfg.worker_heartbeat_interval)


# ── main worker ───────────────────────────────────────────────────────────────

class BugFixWorker:
    def __init__(self, bug_id: str):
        self.bug_id = bug_id
        self._redis = aioredis.from_url(cfg.redis_url, decode_responses=False)
        self._hb_key = cfg.worker_heartbeat_key.format(bug_id=bug_id)

    async def run(self) -> None:
        logger.info("worker started  env=%s  bug_id=%s", cfg.env, self.bug_id)

        # Phase-2 end marker. Fires after Python init + imports + optional
        # agent_ref reexec + LLM model probe — i.e. the worker is *ready to
        # do work*. Paired with orchestrator's `phase=spawn_start` to give
        # exact phase-2 latency without HealthMonitor's 5s poll-cadence
        # noise.
        logger.info(
            "phase_marker phase=worker_ready bug_id=%s t_wall_ms=%d",
            self.bug_id, time.time_ns() // 1_000_000,
        )

        # Start heartbeat as a background task. First SETEX (in the loop
        # body, before any sleep) writes the same unix_ms timestamp, so a
        # consumer that doesn't see this log line can still read the
        # heartbeat value for an approximate worker_ready timestamp.
        hb_task = asyncio.create_task(
            _heartbeat_loop(self._redis, self._hb_key), name="heartbeat"
        )

        try:
            await asyncio.get_event_loop().run_in_executor(None, self._run_graph)
            # The graph reached an end node, so this bug is done in this
            # process — fixed, no_fix, error or R10 alike. Only now may the
            # intra-loop records go (W2.5): keeping them until here is what
            # lets a worker killed AFTER mini's loop — during git apply, push
            # or the CI wait, which is tens of minutes in ver99 — replay the
            # finished loop instead of re-spending its whole trajectory.
            #
            # Deliberately NOT in the `finally`: an exception or a cancellation
            # means the run did NOT finish, and that is exactly when the next
            # incarnation will want these records. The invariant the memo rests
            # on is "a record exists ⇒ the previous process did not finish
            # normally", and this placement is what maintains it.
            purge_run_records(self.bug_id)
        finally:
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass
            # SET the completion key BEFORE clearing the heartbeat so the
            # Monitor never sees "heartbeat gone + completion absent" (it
            # would interpret that as a crash and restart). Both writes are
            # cheap — order is what matters. Fires on EVERY exit path
            # (fixed/no_fix/error/R10) because we're in `finally`.
            completed_key = cfg.worker_completed_key.format(bug_id=self.bug_id)
            try:
                await self._redis.set(
                    completed_key, b"1", ex=cfg.worker_completed_ttl
                )
            except Exception as e:
                logger.warning("failed to set completion key %s: %s", completed_key, e)
            await self._redis.delete(self._hb_key)
            await self._redis.aclose()
            self._cleanup_repo()
            logger.info("worker finished  bug_id=%s", self.bug_id)

    def _cleanup_repo(self) -> None:
        """Remove the temporary repo directory for this bug."""
        repo_path = Path(cfg.repo_base_path) / self.bug_id
        if repo_path.exists():
            shutil.rmtree(repo_path, onerror=_rm_readonly)
            logger.info("cleaned up repo dir: %s", repo_path)
        else:
            logger.debug("repo dir not found, skipping cleanup: %s", repo_path)

    def _run_graph(self) -> None:
        """Build and invoke the agent (synchronous call, runs in executor)."""
        project_web_url = os.environ["project_web_url"]
        provider = GitLabProvider(project_web_url=project_web_url)

        bug_input = BugInput(
            bug_id=self.bug_id,
            provider=provider,
            project_id=os.environ["project_id"],
            project_web_url=project_web_url,
            job_id=os.environ["job_id"],
            # Orchestrator sets this from the failed pipeline's ref. Empty
            # string preserves the legacy "main" default downstream so
            # local-git / standalone callers keep working.
            source_branch=os.environ.get("BUG_SOURCE_BRANCH", ""),
        )

        agent_spec = load_agent_spec(os.getenv("BF_AGENT_CONFIG"))
        served = _check_llm_model(cfg)  # SystemExit on self-hosted mismatch
        # Phase-2 sub-marker. Diff vs worker_ready = BugInput construction
        # + load_agent_spec + the HTTP `GET /v1/models` probe against the
        # self-hosted backend (cloud backends skip the probe → ~0 delta).
        # Under burst, a backend that's already saturated with concurrent
        # LLM calls will make THIS marker the bottleneck — the probe
        # queues behind real inference. Skipped on cloud-backend mode.
        logger.info(
            "phase_marker phase=worker_llm_probe_done bug_id=%s t_wall_ms=%d",
            self.bug_id, time.time_ns() // 1_000_000,
        )
        agent = make_agent(agent_spec, llm_model_served=served)
        logger.info("invoking agent=%s ...", agent.name)
        # Phase-3 markers. fix_start/fix_end bracket agent.fix(); elapsed_ms
        # on fix_end is the same wallclock RunRecord.elapsed_s would round
        # to, but emitted as a log line so a post-processor can compute
        # per-N p50/p95 phase-3 latency without parsing every journal entry.
        _t_fix0 = time.perf_counter()
        logger.info(
            "phase_marker phase=fix_start bug_id=%s t_wall_ms=%d",
            self.bug_id, time.time_ns() // 1_000_000,
        )
        fix_output = agent.fix(bug_input)
        _fix_elapsed_ms = int((time.perf_counter() - _t_fix0) * 1000)
        logger.info(
            "phase_marker phase=fix_end bug_id=%s outcome=%s iterations=%d "
            "elapsed_ms=%d t_wall_ms=%d",
            self.bug_id, fix_output.outcome, fix_output.iterations,
            _fix_elapsed_ms, time.time_ns() // 1_000_000,
        )

        if fix_output.outcome == "error":
            logger.error("agent finished with error: %s", fix_output.error)
        else:
            logger.info("agent finished: outcome=%s iterations=%d",
                        fix_output.outcome, fix_output.iterations)


# ── entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(description="BugFix Worker")
    parser.add_argument("--bug-id", required=True, help="Bug identifier, e.g. BUG-123")
    args = parser.parse_args()

    loop = asyncio.get_running_loop()
    stop: asyncio.Future = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set_result, None)
        except NotImplementedError:
            pass  # Windows

    worker_task = asyncio.create_task(BugFixWorker(args.bug_id).run())
    done, _ = await asyncio.wait(
        [worker_task, stop],
        return_when=asyncio.FIRST_COMPLETED,
    )
    if worker_task.done():
        try:
            worker_task.result()
        except Exception as e:
            logger.error("worker task failed: %s", e)
    else:
        logger.info("stop signal received, cancelling worker…")
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)


if __name__ == "__main__":
    # Derive bug_id early for the log format string.
    _bug_id = "?"
    if "--bug-id" in sys.argv:
        try:
            _bug_id = sys.argv[sys.argv.index("--bug-id") + 1]
        except IndexError:
            pass

    logging.basicConfig(
        level=logging.DEBUG,
        format=f"%(asctime)s %(levelname)s [worker:{_bug_id} %(name)s:%(funcName)s:%(lineno)d] %(message)s",
        stream=sys.stdout,
        force=True,
    )

    # Phase-2 sub-marker. By the time logging.basicConfig returns, every
    # top-level `from agents.base import …` / `from providers… import …`
    # has run — langgraph / openai / langchain all eager-load on import,
    # and that's the biggest chunk of cold-Python-startup cost. So this
    # marker fires exactly when "imports are done, ready to enter main
    # code". Diff vs spawn_start = pure Python startup + library
    # eager-load. Under burst this is the one most likely to balloon
    # from CPU/disk contention as N parallel workers fault in the same
    # modules.
    logger.info(
        "phase_marker phase=worker_imports_done bug_id=%s t_wall_ms=%d",
        _bug_id, time.time_ns() // 1_000_000,
    )

    logger.debug("cfg=%s", cfg)

    _agent_ref_exit = maybe_reexec_for_agent_ref(
        os.getenv("BF_AGENT_CONFIG"),
        "bf_worker/bf_worker.py",
    )
    if _agent_ref_exit is not None:
        raise SystemExit(_agent_ref_exit)

    # Phase-2 sub-marker. Diff vs worker_imports_done = agent_ref reexec
    # cost (effectively 0 when no agent_ref is pinned — the common case;
    # in the reexec child this marker fires from the new process after
    # the child's own check returns None, so a non-zero delta isolates
    # the worktree-creation + child-process restart).
    logger.info(
        "phase_marker phase=worker_agent_ref_done bug_id=%s t_wall_ms=%d",
        _bug_id, time.time_ns() // 1_000_000,
    )

    asyncio.run(main())
