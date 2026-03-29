# uvicorn gateway.gateway:app --host 0.0.0.0 --port 8000
import json
import logging
import sys

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


@app.post("/webhook")
async def webhook(payload: dict):
    cfg, redis_client = _get_state()
    logger.debug(f"{payload=}")

    raw: bytes = json.dumps(payload).encode("utf-8")

    if cfg.use_redis and redis_client is not None:
        redis_client.xadd(cfg.gateway_stream, {"data": raw})
        logger.debug("msg forwarded to stream=%r", cfg.gateway_stream)

    return {"status": "ok"}


