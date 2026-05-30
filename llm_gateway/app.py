"""FastAPI app — OpenAI-compatible passthrough.

Endpoints:
    POST /v1/chat/completions  — select backend via policy, forward upstream
    GET  /v1/models            — return the union of configured models
    GET  /health               — liveness + per-backend last-seen status

Hint headers (worker → gateway):
    X-Sdlcma-Bug-Id: <bug_id>      — correlates a request to a bug
    X-Sdlcma-Attempt: <int>        — react_loop retry counter (0 = first try)

Response header (gateway → worker):
    X-Sdlcma-Backend-Name: <name>  — which backend actually served this call,
                                     so RunRecord.llm_backend_name can be set.

The body forwarded to the upstream has its `model` rewritten to the
backend's declared model — the worker may send any placeholder. This is
the only request transformation; everything else (messages, tools,
temperature, stream, etc.) is preserved byte-for-byte.

Streaming: not supported. The worker uses non-streaming completions. A
streaming request will be forwarded as-is but the response is read fully
before returning, defeating the streaming contract. If the worker ever
moves to streaming, swap `httpx.AsyncClient.post` for `.stream()`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .cache import Cache
from .config import BackendConfig, GatewayConfig, load_config
from .metrics import CACHE_LOOKUPS, UPSTREAM_WALLCLOCK_MS

# Configure logging at module import so the gateway's own INFO logs
# (config loaded, cache enabled, per-request phase_marker lines) actually
# reach stdout when uvicorn imports this module. Uvicorn configures its
# own `uvicorn` / `uvicorn.error` / `uvicorn.access` loggers but does
# NOT touch arbitrary application loggers — without this call our
# `logger.info(...)` would go to the root logger's default handler,
# which is silent below WARNING. `force=True` so a host process that
# pre-configured logging differently still gets the right config when
# running this gateway. Level overridable via LLM_GATEWAY_LOG_LEVEL.
logging.basicConfig(
    level=os.environ.get("LLM_GATEWAY_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [gw %(name)s:%(funcName)s:%(lineno)d] %(message)s",
    stream=sys.stdout,
    force=True,
)
from .keying import derive_key, short_key
from .policy import InferencePolicy, parse_attempt_header

logger = logging.getLogger(__name__)

# Module-level singleton, populated by `_init_app`. FastAPI uses a worker
# pool of one async loop here — single-process by default. Sharing via the
# module global is the simplest correct pattern.
_state: dict[str, Any] = {
    "config": None,        # GatewayConfig
    "policy": None,        # InferencePolicy
    "http": None,          # httpx.AsyncClient
    "backend_health": {},  # name -> {"last_ok_ts": float | None, "last_err": str | None}
    "cache": None,         # llm_gateway.cache.Cache | None
    "cache_request_count": 0,  # for periodic stats summary log
}

# Header names — defined once so the worker and gateway never drift.
HEADER_BUG_ID = "x-sdlcma-bug-id"
HEADER_ATTEMPT = "x-sdlcma-attempt"
HEADER_BACKEND = "x-sdlcma-backend-name"
# Cache-related response headers. The worker doesn't need them today
# (analyze_phase_log parses the gateway's `phase_marker phase=cache_lookup`
# log line instead), but they're useful for ad-hoc curl debugging.
HEADER_CACHE_RESULT = "x-sdlcma-cache"      # hit | miss | disabled
HEADER_CACHE_KEY = "x-sdlcma-cache-key"     # short prefix only

# Upstream transient retry. The worker side has its own `_invoke_llm_with_retry`
# but it can't see which backend served the request, so it can't make a sound
# "this backend is dead" decision. The gateway retries narrowly on transients
# and only then surfaces the failure to the policy. Same shape as the worker
# side: 1 attempt + 2 retries with (1s, 2s).
_TRANSIENT_RETRY_DELAYS = (1.0, 2.0)
_TRANSIENT_STATUS_CODES = (429, 500, 502, 503, 504)


def _init_app(cfg_path: str | None = None) -> FastAPI:
    app = FastAPI(title="sdlcma-llm-gateway")

    @app.on_event("startup")
    async def _startup() -> None:
        path = cfg_path or os.environ.get("LLM_GATEWAY_CONFIG")
        if not path:
            raise RuntimeError(
                "LLM_GATEWAY_CONFIG env var not set (or no path passed to _init_app). "
                "Point it at a YAML/JSON gateway config file."
            )
        cfg = load_config(path)
        _state["config"] = cfg
        _state["policy"] = InferencePolicy(cfg)
        _state["http"] = httpx.AsyncClient(timeout=None)  # per-call timeout set per backend
        _state["backend_health"] = {
            b.name: {"last_ok_ts": None, "last_err": None} for b in cfg.backends
        }
        # Optional cache. Opening the sqlite happens eagerly so a bad path
        # (wrong perms, schema mismatch) fails at startup, not 4 hours
        # into a stress test.
        if cfg.cache.enabled:
            _state["cache"] = Cache(cfg.cache.db_path)
            logger.info(
                "gateway: cache enabled mode=%s db=%s replay_with_latency=%s",
                cfg.cache.mode, cfg.cache.db_path, cfg.cache.replay_with_latency,
            )
        else:
            _state["cache"] = None
        _state["cache_request_count"] = 0
        logger.info(
            "gateway: loaded config %s (%d backend(s), policy=%s, order=%s)",
            path, len(cfg.backends), cfg.policy.type, list(cfg.policy.order),
        )

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        client: httpx.AsyncClient | None = _state.get("http")
        if client is not None:
            await client.aclose()
        cache: Cache | None = _state.get("cache")
        if cache is not None:
            cache.close()

    @app.get("/metrics")
    async def metrics_endpoint():
        """Prometheus scrape endpoint. Explicit GET route (not
        app.mount) to avoid the 307 redirect that mount(/metrics)
        triggers — same anti-friction reasoning as the gateway."""
        return Response(
            content=generate_latest(), media_type=CONTENT_TYPE_LATEST
        )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        cfg: GatewayConfig | None = _state.get("config")
        if cfg is None:
            raise HTTPException(503, "gateway not initialised")
        return {
            "status": "ok",
            "policy": {
                "type": cfg.policy.type,
                "order": list(cfg.policy.order),
                "advance_on_fix_failure": cfg.policy.advance_on_fix_failure,
            },
            "backends": [
                {
                    "name": b.name,
                    "model": b.model,
                    "base_url": b.base_url,
                    "health": _state["backend_health"].get(b.name, {}),
                }
                for b in cfg.backends
            ],
        }

    @app.get("/cache/stats")
    async def cache_stats() -> dict[str, Any]:
        """Live hit/miss/record counters + on-disk size. Cheap to poll;
        the underlying COUNT(*) is fast on the indexed cache_entries
        table even at hundreds of thousands of rows."""
        c: Cache | None = _state.get("cache")
        if c is None:
            return {"enabled": False}
        snap = c.snapshot_stats()
        return {"enabled": True, **snap.to_dict()}

    @app.get("/v1/models")
    async def list_models() -> dict[str, Any]:
        cfg: GatewayConfig | None = _state.get("config")
        if cfg is None:
            raise HTTPException(503, "gateway not initialised")
        # Mimic the OpenAI shape so naive clients (e.g. a generic probe) can
        # parse `data[0].id`. We list every backend's declared model — primary
        # first. The worker's llm_model_check is bypassed when LLM_API_BASE_URL
        # points at the gateway (the gateway can't promise which backend a
        # future request will hit), so this endpoint is informational, not a
        # contract.
        return {
            "object": "list",
            "data": [
                {"id": b.model, "object": "model", "owned_by": b.name}
                for b in cfg.backends
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(
        request: Request,
        x_sdlcma_bug_id: str | None = Header(default=None, alias=HEADER_BUG_ID),
        x_sdlcma_attempt: str | None = Header(default=None, alias=HEADER_ATTEMPT),
    ) -> JSONResponse:
        cfg: GatewayConfig | None = _state.get("config")
        policy: InferencePolicy | None = _state.get("policy")
        client: httpx.AsyncClient | None = _state.get("http")
        if cfg is None or policy is None or client is None:
            raise HTTPException(503, "gateway not initialised")

        attempt = parse_attempt_header(x_sdlcma_attempt)
        bug_id = x_sdlcma_bug_id or None

        raw_body = await request.body()
        try:
            body_json = json.loads(raw_body)
        except ValueError:
            raise HTTPException(400, "request body is not valid JSON")
        if not isinstance(body_json, dict):
            raise HTTPException(400, "request body must be a JSON object")

        # Cache lookup BEFORE backend selection — a hit means we don't
        # consult the policy or burn an upstream call. Key is derived from
        # the body the WORKER sent, not the body after we rewrote `model`
        # (the worker may pass a placeholder model and the gateway picks
        # the real one), so keys stay stable across backend swaps as long
        # as the request's semantic content is the same.
        cache: Cache | None = _state.get("cache")
        cache_cfg = cfg.cache
        cache_key = (
            derive_key(body_json, normalize_content=cache_cfg.normalize_content)
            if cache is not None else ""
        )
        if cache is not None and cache_cfg.mode in ("replay", "cache"):
            entry = cache.get(cache_key)
            if entry is not None:
                # Optional latency replay: sleep to the original recorded
                # wallclock so stress-test phase-3 numbers stay realistic
                # even though we're not paying for tokens. Capped sleep so
                # a pathologically long original call (timeout = 600s)
                # doesn't freeze the gateway.
                if cache_cfg.replay_with_latency and entry.original_wallclock_ms > 0:
                    await asyncio.sleep(min(entry.original_wallclock_ms / 1000.0, 60.0))
                _maybe_emit_cache_summary()
                logger.info(
                    "phase_marker phase=cache_lookup mode=%s result=hit "
                    "key=%s bug_id=%s hit_count=%d original_backend=%s",
                    cache_cfg.mode, short_key(cache_key), bug_id or "",
                    entry.hit_count, entry.original_backend_name or "",
                )
                CACHE_LOOKUPS.labels(result="hit", mode=cache_cfg.mode).inc()
                body = _safe_json(
                    entry.response_body,
                    default={"raw": entry.response_body.decode("utf-8", "replace")},
                )
                return JSONResponse(
                    content=body,
                    status_code=entry.response_status_code,
                    headers={
                        HEADER_BACKEND: entry.original_backend_name or "cache",
                        HEADER_CACHE_RESULT: "hit",
                        HEADER_CACHE_KEY: short_key(cache_key),
                    },
                )
            # Miss in replay mode = strict failure. 409 Conflict so the
            # worker can distinguish "cache gap" from "backend down" (502)
            # by status code alone, without parsing the body.
            if cache_cfg.mode == "replay":
                logger.warning(
                    "phase_marker phase=cache_lookup mode=replay result=miss "
                    "key=%s bug_id=%s (strict replay; returning 409)",
                    short_key(cache_key), bug_id or "",
                )
                CACHE_LOOKUPS.labels(result="miss", mode="replay").inc()
                _maybe_emit_cache_summary()
                return JSONResponse(
                    content={"error": {
                        "message": "cache miss in replay mode",
                        "type": "cache_miss_replay",
                        "cache_key": short_key(cache_key),
                    }},
                    status_code=409,
                    headers={
                        HEADER_CACHE_RESULT: "miss",
                        HEADER_CACHE_KEY: short_key(cache_key),
                    },
                )
            # cache mode + miss → count it, fall through, forward, then store.
            CACHE_LOOKUPS.labels(result="miss", mode=cache_cfg.mode).inc()

        # Single backend selection per request. The policy is stateless: it
        # picks one backend based on `attempt`, the gateway forwards (with
        # narrow transient retry per call), and returns whatever the upstream
        # produced — success or failure. When the chosen backend is genuinely
        # down, the worker's next react_loop entry comes back with attempt+1
        # and the policy advances on its own. Cost of this design = one
        # wasted call per backend outage; benefit = no per-request advance
        # loop, no per-bug state, no unbounded memory.
        sel = policy.select(attempt)
        forward_t0 = time.monotonic()
        outcome = await _forward(
            client=client,
            backend=sel.backend,
            body_json=dict(body_json),  # shallow copy; model is rewritten
            attempt=attempt,
            bug_id=bug_id,
        )
        forward_wallclock_ms = int((time.monotonic() - forward_t0) * 1000)
        # Observe upstream wallclock regardless of success/failure —
        # both shape the operator's view of backend health. Cache hits
        # never reach here, so this histogram is strictly upstream time.
        UPSTREAM_WALLCLOCK_MS.labels(backend=sel.backend.name).observe(
            forward_wallclock_ms
        )

        resp_headers = dict(outcome["headers"])
        resp_headers[HEADER_BACKEND] = sel.backend.name
        # Don't echo hop-by-hop or content-length back — JSONResponse sets
        # its own Content-Length on the re-serialised body.
        for h in ("content-length", "content-encoding", "transfer-encoding"):
            resp_headers.pop(h, None)

        if outcome["kind"] == "ok":
            _mark_backend_ok(sel.backend.name)
            # Cache write: only on real upstream success. We DO NOT cache
            # 4xx/5xx — replaying a stored error would mask real issues
            # on subsequent runs ("the model returned 400 every time" is a
            # bug signal, not state to preserve).
            if cache is not None and cache_cfg.mode in ("record", "cache"):
                try:
                    body_bytes = json.dumps(outcome["body"]).encode("utf-8")
                except (TypeError, ValueError):
                    body_bytes = b""
                if body_bytes:
                    cache.put(
                        cache_key,
                        body_bytes,
                        outcome["status"],
                        forward_wallclock_ms,
                        sel.backend.name,
                    )
                    logger.info(
                        "phase_marker phase=cache_lookup mode=%s result=miss "
                        "key=%s bug_id=%s recorded=1 wallclock_ms=%d backend=%s",
                        cache_cfg.mode, short_key(cache_key), bug_id or "",
                        forward_wallclock_ms, sel.backend.name,
                    )
                    resp_headers[HEADER_CACHE_RESULT] = "miss"
                    resp_headers[HEADER_CACHE_KEY] = short_key(cache_key)
                    # In `record` mode we count miss only here — the
                    # lookup-side path skips for record mode. For
                    # `cache` mode the miss was already counted at the
                    # fall-through, so don't double-count.
                    if cache_cfg.mode == "record":
                        CACHE_LOOKUPS.labels(
                            result="miss", mode="record"
                        ).inc()
            elif cache is not None:
                # disabled-by-mode still emits a "disabled" header so curl
                # can tell cache is wired up but not active.
                resp_headers[HEADER_CACHE_RESULT] = "disabled"
                CACHE_LOOKUPS.labels(result="disabled", mode="disabled").inc()
            else:
                # Cache config absent entirely (cache.enabled=False). Still
                # track for "what % of LLM calls went through gateway" totals.
                CACHE_LOOKUPS.labels(result="disabled", mode="disabled").inc()
            _maybe_emit_cache_summary()
            return JSONResponse(
                content=outcome["body"],
                status_code=outcome["status"],
                headers=resp_headers,
            )

        # Failure: surface the upstream response (or a 502 envelope when the
        # backend was unreachable) so the worker sees a real LLM error.
        _mark_backend_err(sel.backend.name, outcome["error"])
        logger.warning(
            "gateway: backend %s failed (status=%s reason=%s) — returning to worker; "
            "next react_loop attempt will advance",
            sel.backend.name, outcome["status"], outcome["error"],
        )
        if outcome["body_bytes"]:
            body = _safe_json(
                outcome["body_bytes"],
                default={"raw": outcome["body_bytes"].decode("utf-8", "replace")},
            )
            status = outcome["status"]
        else:
            body = {"error": {
                "message": f"backend {sel.backend.name!r} unreachable: {outcome['error']}",
                "type": "gateway_upstream_unreachable",
                "backend": sel.backend.name,
            }}
            status = 502
        return JSONResponse(content=body, status_code=status, headers=resp_headers)

    return app


def _safe_json(data: bytes, default: Any) -> Any:
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return default


def _mark_backend_ok(name: str) -> None:
    _state["backend_health"][name] = {"last_ok_ts": time.time(), "last_err": None}


def _mark_backend_err(name: str, err: str) -> None:
    rec = _state["backend_health"].setdefault(name, {})
    rec["last_err"] = err


def _maybe_emit_cache_summary() -> None:
    """Emit a one-line `phase_marker phase=cache_summary` every
    `log_every_n` requests. Throttled to keep long runs from flooding
    logs with cumulative stats lines; live polling goes via /cache/stats."""
    cfg: GatewayConfig | None = _state.get("config")
    cache: Cache | None = _state.get("cache")
    if cfg is None or cache is None or cfg.cache.log_every_n <= 0:
        return
    _state["cache_request_count"] = int(_state.get("cache_request_count", 0)) + 1
    n = _state["cache_request_count"]
    if n % cfg.cache.log_every_n != 0:
        return
    snap = cache.snapshot_stats()
    logger.info(
        "phase_marker phase=cache_summary requests=%d hits=%d misses=%d "
        "records=%d errors=%d hit_rate=%s entries=%d db_size_bytes=%d",
        n, snap.hits, snap.misses, snap.records, snap.errors,
        f"{snap.hit_rate:.3f}" if snap.hit_rate is not None else "-",
        snap.entry_count, snap.db_size_bytes,
    )


async def _forward(
    *,
    client: httpx.AsyncClient,
    backend: BackendConfig,
    body_json: dict[str, Any],
    attempt: int,
    bug_id: str | None,
) -> dict[str, Any]:
    """Forward one request to one backend with narrow transient retry.

    Returns:
        {"kind": "ok",   "status": int, "headers": dict, "body": json-decoded}
        {"kind": "fail", "status": int, "headers": dict, "body_bytes": bytes,
         "error": short reason string}
    """
    body_json["model"] = backend.model
    url = backend.base_url.rstrip("/") + "/chat/completions"
    headers = {
        "authorization": f"Bearer {backend.api_key}",
        "content-type": "application/json",
        "accept": "application/json",
    }
    # Echo the hint headers upstream too — harmless for backends that
    # ignore them, useful when a backend is itself fronted by another router.
    if bug_id:
        headers[HEADER_BUG_ID] = bug_id
    headers[HEADER_ATTEMPT] = str(attempt)

    delays = _TRANSIENT_RETRY_DELAYS
    attempts = 1 + len(delays)
    last_status = 0
    last_body: bytes = b""
    last_headers: dict[str, str] = {}
    last_err = "unknown"
    for i in range(attempts):
        try:
            resp = await client.post(
                url,
                json=body_json,
                headers=headers,
                timeout=backend.request_timeout,
            )
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout,
                httpx.NetworkError) as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            last_status = 502
            last_body = b""
            last_headers = {}
        except httpx.HTTPError as exc:
            # Other httpx errors are not transient — surface immediately.
            return {
                "kind": "fail",
                "status": 502,
                "headers": {},
                "body_bytes": b"",
                "error": f"{type(exc).__name__}: {exc}",
            }
        else:
            last_status = resp.status_code
            last_body = resp.content
            last_headers = {k.lower(): v for k, v in resp.headers.items()}
            if resp.status_code < 400:
                return {
                    "kind": "ok",
                    "status": resp.status_code,
                    "headers": last_headers,
                    "body": _safe_json(last_body, default={"raw": last_body.decode("utf-8", "replace")}),
                }
            if resp.status_code not in _TRANSIENT_STATUS_CODES:
                # Permanent error (400/401/403/404…) — don't retry, don't
                # advance backend. Return so worker sees the real error.
                return {
                    "kind": "fail",
                    "status": resp.status_code,
                    "headers": last_headers,
                    "body_bytes": last_body,
                    "error": f"upstream {resp.status_code}",
                }
            last_err = f"upstream {resp.status_code}"

        if i == attempts - 1:
            break
        await asyncio.sleep(delays[i])

    return {
        "kind": "fail",
        "status": last_status or 502,
        "headers": last_headers,
        "body_bytes": last_body,
        "error": last_err,
    }


# Module-level app for `uvicorn llm_gateway.app:app`. Reads
# LLM_GATEWAY_CONFIG at startup.
app = _init_app()
