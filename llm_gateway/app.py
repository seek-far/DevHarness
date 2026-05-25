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
import time
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import BackendConfig, GatewayConfig, load_config
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
}

# Header names — defined once so the worker and gateway never drift.
HEADER_BUG_ID = "x-sdlcma-bug-id"
HEADER_ATTEMPT = "x-sdlcma-attempt"
HEADER_BACKEND = "x-sdlcma-backend-name"

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
        logger.info(
            "gateway: loaded config %s (%d backend(s), policy=%s, order=%s)",
            path, len(cfg.backends), cfg.policy.type, list(cfg.policy.order),
        )

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        client: httpx.AsyncClient | None = _state.get("http")
        if client is not None:
            await client.aclose()

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

        # Single backend selection per request. The policy is stateless: it
        # picks one backend based on `attempt`, the gateway forwards (with
        # narrow transient retry per call), and returns whatever the upstream
        # produced — success or failure. When the chosen backend is genuinely
        # down, the worker's next react_loop entry comes back with attempt+1
        # and the policy advances on its own. Cost of this design = one
        # wasted call per backend outage; benefit = no per-request advance
        # loop, no per-bug state, no unbounded memory.
        sel = policy.select(attempt)
        outcome = await _forward(
            client=client,
            backend=sel.backend,
            body_json=dict(body_json),  # shallow copy; model is rewritten
            attempt=attempt,
            bug_id=bug_id,
        )

        resp_headers = dict(outcome["headers"])
        resp_headers[HEADER_BACKEND] = sel.backend.name
        # Don't echo hop-by-hop or content-length back — JSONResponse sets
        # its own Content-Length on the re-serialised body.
        for h in ("content-length", "content-encoding", "transfer-encoding"):
            resp_headers.pop(h, None)

        if outcome["kind"] == "ok":
            _mark_backend_ok(sel.backend.name)
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
