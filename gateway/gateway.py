# uvicorn gateway.gateway:app --host 0.0.0.0 --port 8000
import asyncio
import json
import logging
import sys
import time

import redis
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from gateway import metrics, webhook_auth

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s [gw %(name)s:%(funcName)s:%(lineno)d] %(message)s",        
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)

# Module-level app; side effects (redis connection, config loading) are deferred to the first request
app = FastAPI()


# Prometheus scrape endpoint on the same uvicorn port. Explicit GET route
# (rather than `app.mount("/metrics", make_asgi_app())`) because mount
# redirects `/metrics` → `/metrics/` with 307. Prometheus DOES follow
# redirects, but the extra hop is wasted, curl -s without -L sees an
# empty body (confusing during testing), and dashboards that hardcode
# the no-slash path break. generate_latest() renders the default
# registry into Prometheus text format; CONTENT_TYPE_LATEST is the
# correct media type with the format version that scrapers parse.
@app.get("/metrics")
async def metrics_endpoint():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

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


async def _enforce_webhook_auth(cfg, authorization: str | None, payload: dict) -> None:
    """Gate the webhook when `webhook_auth_mode` is on. No-op otherwise.

    `getattr` rather than attribute access: this must not break test doubles
    or an older env file that predates the setting — an absent field means
    the pre-auth behaviour, which is exactly what `none` means.

    Runs in a worker thread because a cold JWKS fetch is a blocking HTTPS
    call; the warm path (a cached key set) is pure CPU and returns at once.
    """
    mode = (getattr(cfg, "webhook_auth_mode", "none") or "none").strip().lower()
    if mode == "none":
        return
    if mode != "oidc":
        # Fail closed on a typo'd mode. Treating an unrecognised value as
        # "off" would turn a one-character mistake into a silently
        # unauthenticated gateway.
        metrics.WEBHOOK_AUTH_REJECTED.labels(reason="misconfigured").inc()
        logger.error(
            "phase_marker phase=webhook_auth result=reject reason=misconfigured "
            "detail=unknown_webhook_auth_mode mode=%s", mode,
        )
        raise HTTPException(500, f"unknown webhook_auth_mode {mode!r}")

    try:
        claims = await asyncio.to_thread(
            webhook_auth.authenticate_and_authorize, authorization, payload, cfg
        )
    except (
        webhook_auth.WebhookAuthError,
        webhook_auth.WebhookAuthzError,
        webhook_auth.WebhookAuthConfigError,
    ) as exc:
        status = {
            webhook_auth.WebhookAuthError: 401,
            webhook_auth.WebhookAuthzError: 403,
            webhook_auth.WebhookAuthConfigError: 500,
        }[type(exc)]
        metrics.WEBHOOK_AUTH_REJECTED.labels(reason=exc.reason).inc()
        # The audit line for a refused trigger. Deliberately a single
        # phase_marker so the same post-processor that rebuilds per-bug
        # timelines also sees what never became a bug.
        logger.warning(
            "phase_marker phase=webhook_auth result=reject reason=%s status=%d detail=%s",
            exc.reason, status, exc,
        )
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        raise HTTPException(status, str(exc), headers=headers) from exc

    # Accepted: record WHO triggered this run. bug_id does not exist yet
    # (the orchestrator mints it on spawn), so `project_id` + `sub` are the
    # join keys back to the RunRecord this webhook eventually produces.
    logger.info(
        "phase_marker phase=webhook_auth result=accept project_id=%s sub=%s",
        claims.get("project_id", ""), claims.get("sub", ""),
    )


@app.post("/webhook")
async def webhook(payload: dict, authorization: str | None = Header(default=None)):
    _t0 = time.perf_counter()
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

    # Bump received counter immediately so even a Redis failure shows up
    # as a `received - forwarded` gap on the dashboard.
    object_kind = payload.get("object_kind", "unknown") if isinstance(payload, dict) else "unknown"
    classification = metrics.classify_webhook(payload)
    metrics.WEBHOOKS_RECEIVED.labels(
        object_kind=object_kind, classification=classification
    ).inc()

    # Authn (Step 1) + authz (Step 2) run AFTER the received counter so the
    # denominator stays "every POST that reached us", and BEFORE the XADD so a
    # refused request never reaches the orchestrator — the whole point is that
    # no worker is spawned and no LLM budget is spent on it.
    await _enforce_webhook_auth(cfg, authorization, payload)

    if cfg.use_redis and redis_client is not None:
        # `maxlen=N, approximate=True` enforces a soft cap on every write
        # (`MAXLEN ~ N` in RESP). Approximate trim — actual length floats
        # in [N, N+a few hundred] — has constant-time cost regardless of
        # stream size and is dramatically cheaper than `MAXLEN = N`. The
        # cap is the gateway's job (producer-side) so orchestrator
        # restarts never need to play catch-up; see gateway_stream_maxlen
        # docstring in gateway_settings.py for the headroom rationale.
        redis_client.xadd(
            cfg.gateway_stream,
            {"data": raw},
            maxlen=cfg.gateway_stream_maxlen,
            approximate=True,
        )
        metrics.WEBHOOKS_FORWARDED.inc()
        logger.debug("msg forwarded to stream=%r (maxlen~%d)",
                     cfg.gateway_stream, cfg.gateway_stream_maxlen)

    metrics.WEBHOOK_HANDLE_MS.observe((time.perf_counter() - _t0) * 1000)
    return {"status": "ok"}


