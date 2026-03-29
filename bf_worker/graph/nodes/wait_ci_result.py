"""
Node: wait_ci_result

Blocks on the worker's Redis inbox stream (XREADGROUP) until a CI pipeline
result message arrives whose status is terminal.

This node is called from synchronous LangGraph context, so it runs its own
temporary asyncio event loop.  If you migrate the whole graph to async later,
replace _run_async() with a plain `await`.
"""

from __future__ import annotations
import asyncio
import json
import logging
import time
import os
import sys

import redis.asyncio as aioredis

from graph.state import BugFixState

from pathlib import Path
sys.path.append(str(Path.cwd().parent))
from settings import worker_cfg as cfg

logger = logging.getLogger(__name__)

# Statuses that mean the pipeline finished (one way or another).
_TERMINAL = {"success", "failed"}
# Statuses we treat as "still in progress" — keep waiting.
_TRANSIENT = {"pending", "running", "canceled", "skipped", "created", "waiting_for_resource", "preparing"}


async def _wait(bug_id: str, timeout: int) -> str | None:
    inbox_stream = cfg.worker_inbox_stream_key.format(bug_id=bug_id)
    inbox_group  = cfg.worker_inbox_group
    consumer     = cfg.worker_inbox_consumer
    block_ms     = min(cfg.stream_block_ms, cfg.worker_heartbeat_interval * 1000)

    r = aioredis.from_url(cfg.redis_url, decode_responses=False)
    try:
        # Ensure the consumer group exists.
        try:
            await r.xgroup_create(inbox_stream, inbox_group, id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            actual_block = min(block_ms, remaining_ms)

            try:
                results = await r.xreadgroup(
                    groupname=inbox_group,
                    consumername=consumer,
                    streams={inbox_stream: ">"},
                    count=1,
                    block=actual_block,
                )
            except Exception as exc:
                logger.error("xreadgroup error: %s", exc)
                await asyncio.sleep(1)
                continue

            if not results:
                continue

            for _stream, entries in results:
                for entry_id, fields in entries:
                    raw: bytes = fields.get(b"data") or fields.get("data", b"")
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        logger.error("malformed msg id=%s: %s", entry_id, exc)
                        await r.xack(inbox_stream, inbox_group, entry_id)
                        continue

                    await r.xack(inbox_stream, inbox_group, entry_id)
                    status = msg.get("object_attributes", {}).get("status", "")
                    logger.debug("ci msg status=%s", status)

                    if status in _TERMINAL:
                        return status
                    elif status not in _TRANSIENT:
                        logger.warning("unexpected ci status=%s", status)
        return None
    finally:
        await r.aclose()


def wait_ci_result(state: BugFixState, timeout: int = 300) -> BugFixState:
    logger.info("waiting for CI result (timeout=%ds) bug=%s", timeout, state["bug_id"])
    status = asyncio.run(_wait(state["bug_id"], timeout))

    if status is None:
        logger.warning("CI wait timed out")
        return {"ci_status": "timeout"}

    logger.info("CI result: %s", status)
    return {"ci_status": status}
