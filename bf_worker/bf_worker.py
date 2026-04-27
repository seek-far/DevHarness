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

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

from graph.builder import build_graph
from graph.state import BugFixState
from providers.gitlab_provider import GitLabProvider

from pathlib import Path
sys.path.append(str(Path.cwd()))

from settings import worker_cfg as cfg

def _rm_readonly(func, path, exc_info):
    """Error handler for shutil.rmtree: clear read-only flag and retry."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


# ── heartbeat ──────────────────────────────────────────────────────────────────

async def _heartbeat_loop(r: aioredis.Redis, hb_key: str) -> None:
    while True:
        await r.setex(hb_key, cfg.worker_heartbeat_ttl * 2, "alive")
        logger.debug("heartbeat refreshed key=%s ttl=%ds", hb_key, cfg.worker_heartbeat_ttl)
        await asyncio.sleep(cfg.worker_heartbeat_interval)


# ── main worker ───────────────────────────────────────────────────────────────

class BugFixWorker:
    def __init__(self, bug_id: str):
        self.bug_id = bug_id
        self._redis = aioredis.from_url(cfg.redis_url, decode_responses=False)
        self._hb_key = cfg.worker_heartbeat_key.format(bug_id=bug_id)

    async def run(self) -> None:
        logger.info("worker started  env=%s  bug_id=%s", cfg.env, self.bug_id)

        # Start heartbeat as a background task.
        hb_task = asyncio.create_task(
            _heartbeat_loop(self._redis, self._hb_key), name="heartbeat"
        )

        try:
            await asyncio.get_event_loop().run_in_executor(None, self._run_graph)
        finally:
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass
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
        """Build and invoke the LangGraph (synchronous call, runs in executor)."""
        project_web_url = os.environ["project_web_url"]
        provider = GitLabProvider(project_web_url=project_web_url)

        initial_state: BugFixState = {
            "provider":        provider,
            "bug_id":          self.bug_id,
            "project_id":      os.environ["project_id"],
            "project_web_url": project_web_url,
            "job_id":          os.environ["job_id"],
            # counters start at zero
            "llm_retry_count": 0,
            "fix_retry_count": 0,
            # everything else starts as None / absent
        }

        graph = build_graph()
        logger.info("graph compiled, invoking…")
        final_state: BugFixState = graph.invoke(initial_state)

        if final_state.get("error"):
            logger.error("graph finished with error: %s", final_state["error"])
        else:
            logger.info("graph finished successfully")


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
    
    logger.debug("cfg=%s", cfg)
        
    asyncio.run(main())
