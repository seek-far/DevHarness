#!/usr/bin/env python3
"""
End-to-end integration test v3: Gateway → Redis Stream → Orchestrator → Worker.

Data flow:
  1. httpx POST /webhook  →  the real gateway app writes the stream (data field, JSON bytes)
  2. Orchestrator consumes stream → parse → spawn Worker subprocess
  3. Worker starts, sends heartbeat, runs fix logic, waits for validation
  4. httpx POST /webhook sends validation result → gateway → stream → orchestrator → worker inbox
  5. Worker receives validation, exits normally (return code 0)
  6. Assertions: dead-letter is empty, pending count is 0, worker exited cleanly

Prerequisites:
  - Redis is running, default redis://127.0.0.1:6379/15 (db=15 isolated)
  - Dependencies: pip install fastapi uvicorn redis pydantic-settings httpx

Usage:
  uv run python integration_test.py
  uv run python integration_test.py --redis-url redis://127.0.0.1:6379/15 --bug-id BUG-IT-1

Note: Use `uv run python` rather than calling a venv interpreter directly.
`apply_change_and_test` shells out to `python -m venv` to create an isolated
test environment, which requires `python` (not just `python3`) on PATH.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import redis as sync_redis
import redis.asyncio as aioredis

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s [int_test %(name)s:%(funcName)s:%(lineno)d] %(message)s",
    stream=sys.stdout,
)

# Ensure the project root directory is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Import the real gateway app and the override injection function
from gateway.gateway import app as gateway_app, override as gateway_override
from gateway.gateway_settings import GatewaySettings
from orchestrator.orchestrator import Orchestrator
from settings.orchestrator_settings import OrchestratorSettings

logger = logging.getLogger(__name__)


class TestFailure(RuntimeError):
    pass


# ── Utilities ─────────────────────────────────────────────────────

async def wait_for(
    predicate,
    *,
    timeout: float,
    interval: float = 0.2,
    description: str,
):
    """Repeatedly call predicate() until it returns a truthy value or times out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    raise TestFailure(f"Timed out waiting for: {description}")


# ── Main flow ─────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(description="v3 integration test")
    parser.add_argument(
        "--redis-url",
        default=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/15"),
        help="Redis URL (default redis://127.0.0.1:6379/15, db=15 isolated for testing)",
    )
    parser.add_argument("--bug-id", default="BUG-IT-1")
    args = parser.parse_args()

    redis_url: str = args.redis_url
    bug_id: str = args.bug_id

    # ── 1. Build test-specific configuration ─────────────────────

    # Orchestrator config: inject redis_url and shorten all time parameters
    # If OrchestratorSettings reads from an env file, will parameters passed to model_validate override it?
    # Yes — explicit arguments > environment variables/.env > defaults.
    orch_cfg = OrchestratorSettings.model_validate({
        "redis_url": redis_url,
        "worker_heartbeat_interval": 10,
        "worker_heartbeat_ttl": 30,
        "health_check_interval": 20,
        "stream_block_ms": 1000,
        "stream_count": 10,
    })
    logger.debug(f"{orch_cfg=}")
    
    # Gateway config: stream key must match orch_cfg
    gw_cfg = GatewaySettings.model_validate({
        "use_redis": True,
        "redis_url": redis_url,
        "gateway_stream": orch_cfg.gateway_stream,
    })
    logger.debug(f"{gw_cfg=}")

    logger.info(
        "config: redis_url=%s  gateway_stream=%s  bug_id=%s",
        redis_url, gw_cfg.gateway_stream, bug_id,
    )

    # ── 2. Connect to Redis, flush database ──────────────────────

    async_redis = aioredis.from_url(redis_url, decode_responses=False)
    try:
        await async_redis.ping()
    except Exception as exc:
        raise TestFailure(
            f"Cannot connect to Redis at {redis_url!r}: {exc}\n"
            "Start Redis first, e.g.:  docker run --rm -p 6379:6379 redis:7"
        ) from exc

    await async_redis.flushdb()
    logger.info("Redis db flushed")

    # Sync redis client for gateway use (consistent with original gateway.py)
    sync_redis_client = sync_redis.from_url(redis_url, decode_responses=False)

    # ── 3. Inject config into the real gateway app ───────────────
    # gateway_override replaces the module-level _cfg and _redis_client,
    # making gateway_app (the real FastAPI app) use the test-specific redis_url and stream key
    # instead of reading from .env files.
    gateway_override(gw_cfg, sync_redis_client)
    logger.info("gateway override applied: stream=%s", gw_cfg.gateway_stream)

    # ── 4. Start Orchestrator ─────────────────────────────────────

    orch = Orchestrator(settings=orch_cfg)
    orch._monitor.start()
    orch._consumer.start()
    logger.info("Orchestrator started")

    # ── 5. Execute test flow ──────────────────────────────────────
    json_file = "pipeline_msg.txt"
    with open(json_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    #data = {"type": "bug_reported", "bug_id": bug_id}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway_app),
            base_url="http://testserver",
        ) as client:

            # Step A: POST bug_reported → real gateway app → stream → orchestrator
            logger.info("[Step A] POST bug_reported")
            resp = await client.post(
                "/webhook",
                json=data,
            )
            assert resp.status_code == 200, f"gateway returned {resp.status_code}: {resp.text}"
            assert resp.json() == {"status": "ok"}
            logger.info("[Step A] gateway responded: %s", resp.json())

            # Step B: Wait for orchestrator to spawn worker
            logger.info("[Step B] waiting for worker to be registered ...")

            # async def worker_registered():
            #     return orch._registry.get(bug_id)
            # entry = await wait_for(
            #     worker_registered, timeout=10, description="worker registration"
            # )
            
            # debug only
            # await asyncio.sleep(10)
            # print(orch._registry.all_active())
            # return
            
            async def get_all_worker_entries():
                return orch._registry.all_active()
                
            all_worker_entries = await wait_for(
                get_all_worker_entries, timeout=10, description="worker registration"
            )
            entry = next(iter(all_worker_entries.values()))
            bug_id = entry.bug_id
            logger.info("[Step B] worker registered pid=%s", entry.pid)

            # Step C: Wait for worker heartbeat
            logger.info("[Step C] waiting for worker heartbeat ...")
            heartbeat_key = orch_cfg.worker_heartbeat_key.format(bug_id=bug_id)

            async def heartbeat_present():
                ttl = await async_redis.ttl(heartbeat_key)
                return ttl if ttl > 0 else None

            initial_ttl = await wait_for(
                heartbeat_present, timeout=120, description="worker heartbeat"
            )
            logger.info("[Step C] heartbeat present ttl=%s", initial_ttl)

            # Step D: worker needs ~4 seconds to complete step1+step2;
            # wait for it to enter _wait_for_validation before sending validation
            logger.info("[Step D] waiting for worker to enter validation wait (~5min) ...")
            await asyncio.sleep(60)

            # Step E: POST validation → real gateway app → stream → orchestrator → worker inbox
            logger.info("[Step E] POST bug_fix_validation_status (passed)")
            resp = await client.post(
                "/webhook",
                json={
                    "object_attributes": {
                        "ref": f"auto/bug_{bug_id}-patch_17_50_41_3",
                        "status": "success"
                    },
                #    "type": "bug_fix_validation_status",
                #    "bug_id": bug_id,
                #    "status": "passed",
                },
            )
            assert resp.status_code == 200, f"gateway returned {resp.status_code}: {resp.text}"
            logger.info("[Step E] gateway responded: %s", resp.json())

            # Step F: Wait for worker process to exit normally
            logger.info("[Step F] waiting for worker process to exit ...")
            return_code = await asyncio.wait_for(entry.process.wait(), timeout=600)
            if return_code != 0:
                raise TestFailure(f"Worker exited with non-zero code: {return_code}")
            logger.info("[Step F] worker exited cleanly return_code=%s", return_code)

        # ── 6. Assertions ─────────────────────────────────────────

        await asyncio.sleep(0.5)  # give ack a moment to complete

        dead_letter_len = await async_redis.xlen(orch_cfg.dead_letter_stream)

        gateway_pending = 0
        try:
            info = await async_redis.xpending(
                orch_cfg.gateway_stream, orch_cfg.gateway_consumer_group
            )
            gateway_pending = info.get("pending", 0) if info else 0
        except Exception:
            pass

        worker_inbox_pending = 0
        try:
            inbox_stream = orch_cfg.worker_inbox_stream_key.format(bug_id=bug_id)
            info = await async_redis.xpending(inbox_stream, orch_cfg.worker_inbox_group)
            worker_inbox_pending = info.get("pending", 0) if info else 0
        except Exception:
            pass

        heartbeat_ttl_after = await async_redis.ttl(heartbeat_key)

        result: dict[str, Any] = {
            "ok": True,
            "redis_url": redis_url,
            "bug_id": bug_id,
            "worker_pid": entry.pid,
            "initial_heartbeat_ttl": initial_ttl,
            "worker_return_code": return_code,
            "dead_letter_stream_len": dead_letter_len,
            "gateway_pending_count": gateway_pending,
            "worker_inbox_pending_count": worker_inbox_pending,
            "heartbeat_ttl_after_exit": heartbeat_ttl_after,
        }

        if dead_letter_len != 0:
            raise TestFailure(f"dead-letter stream not empty: len={dead_letter_len}")
        if gateway_pending != 0:
            raise TestFailure(f"gateway stream has unacked messages: {gateway_pending}")
        if worker_inbox_pending != 0:
            raise TestFailure(f"worker inbox has unacked messages: {worker_inbox_pending}")

        print(json.dumps(result, ensure_ascii=False, indent=2))

    finally:
        # ── Cleanup ───────────────────────────────────────────────
        try:
            await orch._consumer.stop()
        except Exception:
            pass
        try:
            await orch._monitor.stop()
        except Exception:
            pass
        try:
            e = orch._registry.get(bug_id)
            if e and e.process.returncode is None:
                e.process.terminate()
                try:
                    await asyncio.wait_for(e.process.wait(), timeout=3)
                except Exception:
                    e.process.kill()
        except Exception:
            pass

        await async_redis.flushdb()
        await async_redis.aclose()
        sync_redis_client.close()
        logger.info("cleanup done")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except TestFailure as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
