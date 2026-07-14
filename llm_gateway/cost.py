"""Turn an upstream response's `usage` block into money.

The gateway is the right place for this: it is the only component that knows
BOTH which backend actually served the request and what that backend costs.
The worker can't — the policy picks the backend after the worker has already
sent the request, and in a fallback ladder the same run may touch two backends
at different prices.

Cost is handed back to the worker in the `X-Sdlcma-Cost-Usd` response header,
which the worker's existing httpx event hook already knows how to read (it is
the same seam that captures `X-Sdlcma-Backend-Name`). From there it lands on
`RunRecord.total_cost_usd` and is debited against the run budget.

Cache hits cost NOTHING. They must never touch the spend counter — they go to
`cost_saved` instead, which is the number that actually justifies the cache.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .config import BackendConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0


def extract_usage(body: Any) -> Usage | None:
    """Pull token counts out of an OpenAI-shaped response. None when absent.

    None is meaningfully different from a zeroed Usage: it means the backend
    told us nothing, so we must not claim a cost of $0.00.
    """
    if not isinstance(body, dict):
        return None
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None

    details = usage.get("prompt_tokens_details")
    cached = 0
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        cached = int(details["cached_tokens"] or 0)

    return Usage(
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        cached_tokens=cached,
    )


def cost_of(backend: BackendConfig, usage: Usage | None) -> float | None:
    """Cost in the backend's currency, or None when it can't be known.

    Returns None — not 0.0 — when the backend declares no pricing or the
    upstream reported no usage. A fabricated $0.00 in a cost dashboard is worse
    than an honest blank: it reads as "this was free".
    """
    if usage is None or backend.pricing is None:
        return None
    return backend.pricing.cost_usd(
        usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens
    )
