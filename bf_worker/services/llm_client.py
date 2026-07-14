"""Centralised ChatOpenAI factory.

Two responsibilities:

1. Build a `ChatOpenAI` instance from worker settings — replacing the
   module-level construction that previously lived in react_loop.py and
   the lazy construction in enhancements/reflection.py. Building per-call
   is cheap (single object instantiation) and lets us attach per-call hint
   headers (X-Sdlcma-Bug-Id, X-Sdlcma-Attempt) when the LLM endpoint is
   an SDLCMA llm_gateway.

2. Capture the gateway's response header `X-Sdlcma-Backend-Name` on each
   call, so RunRecord.llm_backend_name can record which configured backend
   actually served this run.

Direct-to-backend path (cfg.llm_via_gateway is False): no headers, no
event hook, behaviour byte-identical to the previous module-level
ChatOpenAI. The single extra construction per react_loop entry is the
only behavioural difference, and it is negligible (~ms).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Mapping

from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)

# Gateway → worker telemetry hand-off. Each LLM call's response carries an
# `X-Sdlcma-Backend-Name` header naming the upstream backend the gateway
# routed to. We stash it in a thread-local so the calling node can read it
# right after `.invoke()` returns, without depending on ChatOpenAI exposing
# response headers (which it does not).
#
# Thread-local (not module-global) so concurrent react_loop entries inside
# one worker process — should that ever happen, e.g. a future parallel
# code-review path — don't trample each other. Today the worker is single-
# threaded for LLM calls, but the future-proofing is free.
_BACKEND_TLS = threading.local()

HEADER_BUG_ID = "X-Sdlcma-Bug-Id"
HEADER_ATTEMPT = "X-Sdlcma-Attempt"
HEADER_BACKEND = "x-sdlcma-backend-name"  # response header (case-insensitive on read)
# Per-call cost, as computed by the gateway. The gateway is authoritative: it is
# the only party that knows which backend the policy selected and what that
# backend charges — and in a fallback ladder one run can touch two backends at
# two prices. Absent when the backend declares no pricing, in which case the
# cost stays UNKNOWN (None), which is not the same as free.
HEADER_COST = "x-sdlcma-cost-usd"


def _make_event_hook_client(timeout: float):
    """Build an httpx.Client that captures the gateway's telemetry headers.

    Only used when the worker is talking to an llm_gateway. For a direct
    cloud / self-hosted backend we want zero httpx-level interference (the
    OpenAI SDK builds its own client with retry / proxy / etc. defaults).
    """
    import httpx  # local import so workers without llm_gateway don't import httpx eagerly

    def _on_response(resp: "httpx.Response") -> None:
        # Header lookup is case-insensitive in httpx; missing → None → we
        # leave the TLS as-is (last-seen-wins is the contract).
        name = resp.headers.get(HEADER_BACKEND)
        if name:
            _BACKEND_TLS.value = name
        raw_cost = resp.headers.get(HEADER_COST)
        # Reset to None (not 0.0) when the header is absent: "the gateway didn't
        # tell us" must stay distinguishable from "this call was free".
        _BACKEND_TLS.cost = _parse_cost(raw_cost)

    return httpx.Client(
        timeout=timeout,
        event_hooks={"response": [_on_response]},
    )


def _parse_cost(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def resolve_api_key(cfg) -> str:
    """The string that goes in the OpenAI client's `api_key` slot.

    `entra` mode puts a Microsoft Entra access token there instead of a static
    key. That works — and needs no HTTP-layer special case — because the OpenAI
    client sends `Authorization: Bearer <api_key>`, which is exactly what
    Azure's `/openai/v1/` route expects. Token caching / refresh lives in
    services.azure_auth.
    """
    if getattr(cfg, "llm_auth_mode", "api_key") == "entra":
        from services.azure_auth import get_entra_token  # lazy: azure-identity is optional

        return get_entra_token(
            getattr(cfg, "llm_entra_scope", ""),
            getattr(cfg, "llm_entra_client_id", "") or "",
        )
    return cfg.llm_api_key


def apply_param_profile(kwargs: dict[str, Any], cfg) -> dict[str, Any]:
    """Drop request params the configured backend rejects.

    `reasoning` backends (o-series, gpt-5 family) reject `temperature`
    OUTRIGHT — Azure answers HTTP 400 `unsupported_value` with "Only the
    default (1) value is supported" (verified live 2026-07-14 against
    gpt-5-mini). Omitting the param is correct; pinning it to 1 would be
    accepted but is pointless, and 0 is simply refused.

    Mutates and returns `kwargs` so callers can chain.
    """
    if getattr(cfg, "llm_param_profile", "chat") == "reasoning":
        kwargs.pop("temperature", None)
    return kwargs


def build_llm_with_headers(
    *,
    bug_id: str | None,
    attempt: int,
    tools: list | None = None,
):
    """Construct ChatOpenAI for this react_loop entry.

    `bug_id` / `attempt` become hint headers ONLY when the worker is
    configured for gateway mode (cfg.llm_via_gateway). In direct-backend
    mode we skip the headers AND the event-hook httpx.Client, so the
    network path is identical to the pre-gateway code.

    `tools` is the optional bind_tools schema (react_loop uses it;
    reflection does not).
    """
    # Late import so this module is importable without the settings tree
    # being fully constructed (tests sometimes monkey-patch settings).
    from settings import worker_cfg as cfg

    via_gateway = bool(getattr(cfg, "llm_via_gateway", False))

    kwargs: dict[str, Any] = dict(
        api_key=resolve_api_key(cfg),
        base_url=cfg.llm_api_base_url,
        model=cfg.llm_model,
        temperature=0,
        timeout=cfg.llm_request_timeout,
    )
    apply_param_profile(kwargs, cfg)
    if via_gateway:
        headers: dict[str, str] = {HEADER_ATTEMPT: str(int(attempt))}
        if bug_id:
            headers[HEADER_BUG_ID] = bug_id
        kwargs["default_headers"] = headers
        # Reset TLS so a stale value from a prior call can't be misattributed
        # to this entry if the response header is absent for some reason.
        _BACKEND_TLS.value = None
        kwargs["http_client"] = _make_event_hook_client(cfg.llm_request_timeout)

    llm = ChatOpenAI(**kwargs)
    if tools is not None:
        llm = llm.bind_tools(tools)
    return llm


def read_last_seen_backend() -> str | None:
    """Return the most recent gateway-reported backend name, or None.

    Returns None when (a) the worker is in direct-backend mode, (b) the
    gateway never sent the header (e.g. gateway exhausted all backends),
    or (c) no LLM call has happened yet in this thread.
    """
    return getattr(_BACKEND_TLS, "value", None)


def read_last_seen_cost() -> float | None:
    """Cost of the most recent LLM call as reported by the gateway, or None.

    None in direct-backend mode (there the caller prices the call itself from
    `services.pricing`), and None when the gateway's backend declares no
    pricing. Never 0.0 as a stand-in for "unknown".
    """
    return getattr(_BACKEND_TLS, "cost", None)


def cost_of_call(cfg, prompt_tokens: int, completion_tokens: int,
                 cached_tokens: int = 0) -> float | None:
    """Cost of one LLM call, from whichever source is authoritative here.

    Gateway mode → whatever the gateway charged us (it knows the backend).
    Direct mode  → priced locally from configs/pricing.yaml by LLM_MODEL.
    Unknown model / unpriced backend → None. Never a guess.
    """
    if getattr(cfg, "llm_via_gateway", False):
        return read_last_seen_cost()
    from services.pricing import cost_for  # local import: keeps yaml optional

    return cost_for(
        getattr(cfg, "llm_model", "") or "",
        prompt_tokens, completion_tokens, cached_tokens,
    )
