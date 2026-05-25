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


def _make_event_hook_client(timeout: float):
    """Build an httpx.Client that captures the gateway's backend-name header.

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

    return httpx.Client(
        timeout=timeout,
        event_hooks={"response": [_on_response]},
    )


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
        api_key=cfg.llm_api_key,
        base_url=cfg.llm_api_base_url,
        model=cfg.llm_model,
        temperature=0,
        timeout=cfg.llm_request_timeout,
    )
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
