# uvicorn gateway.gateway:app --host 0.0.0.0 --port 8000
import json
import logging
import sys
import time

import redis
from fastapi import FastAPI

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s [gw %(name)s:%(funcName)s:%(lineno)d] %(message)s",        
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)

# Module-level app; side effects (redis connection, config loading) are deferred to the first request
app = FastAPI()

# Runtime state: lazily initialized by _get_state(), or injected by tests via override()
_redis_client = None
_cfg = None


def _get_state():
    """Lazy initialization: read config and build Redis connection on first request."""
    global _redis_client, _cfg
    if _cfg is None:
        from gateway.gateway_settings import gateway_config
        _cfg = gateway_config
        logger.debug(f"{_cfg=}")
        if _cfg.use_redis:
            _redis_client = redis.from_url(_cfg.redis_url, decode_responses=False)
            logger.debug(f"redis_client={_redis_client}")
    return _cfg, _redis_client


def override(cfg, redis_client):
    """
    Test-only: inject custom config and redis_client after importing app,
    bypassing .env file reading and real connection initialization.
    """
    global _cfg, _redis_client
    _cfg = cfg
    _redis_client = redis_client


@app.get("/healthz")
async def healthz():
    # Liveness/readiness signal: process is up and FastAPI is serving.
    # Intentionally does NOT touch Redis — Redis is lazily connected on the
    # first /webhook call. A "Redis ready" check would belong to a separate
    # /readyz endpoint if we ever want to gate traffic on that.
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(payload: dict):
    cfg, redis_client = _get_state()
    logger.debug(f"{payload=}")

    raw: bytes = json.dumps(payload).encode("utf-8")

    # Phase-1 marker. bug_id doesn't exist yet (orchestrator mints it on
    # spawn). job_id + ref join this line to the downstream
    # `phase=spawn_start` line emitted by orchestrator on the same event.
    # Stays a single line so a post-processor can grep `phase_marker`
    # across all three services and rebuild per-bug timelines.
    ref = (payload.get("object_attributes") or {}).get("ref", "")
    builds = payload.get("builds") or [{}]
    job_id = builds[0].get("id", "") if builds else ""
    logger.info(
        "phase_marker phase=gateway_received job_id=%s ref=%s t_wall_ms=%d",
        job_id, ref, time.time_ns() // 1_000_000,
    )

    if cfg.use_redis and redis_client is not None:
        redis_client.xadd(cfg.gateway_stream, {"data": raw})
        logger.debug("msg forwarded to stream=%r", cfg.gateway_stream)

    return {"status": "ok"}


