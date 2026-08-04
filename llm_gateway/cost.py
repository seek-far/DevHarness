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
    #: BILLABLE output tokens — reasoning folded in when the backend reports it
    #: separately. See _fold_reasoning for why this is not always what the
    #: backend put in `completion_tokens`.
    completion_tokens: int = 0
    cached_tokens: int = 0
    #: Informational: the raw reasoning/thinking count, whichever dialect the
    #: backend speaks. Never added to completion_tokens twice.
    reasoning_tokens: int = 0


def _fold_reasoning(prompt: int, completion: int, reasoning: int, total: Any) -> int:
    """Return the billable completion count, folding in reasoning if it is extra.

    There are two dialects of `completion_tokens_details.reasoning_tokens` in
    the wild and they disagree about the same field:

      * **OpenAI / Azure** — `completion_tokens` ALREADY INCLUDES reasoning;
        the details block is a breakdown. `total = prompt + completion`.
      * **Vertex AI / Gemini** — `completion_tokens` EXCLUDES reasoning; the
        thinking tokens are an additional charge at the output rate.
        `total = prompt + completion + reasoning`.

    Adding reasoning unconditionally double-bills Azure; never adding it
    under-bills Vertex. Measured on gemini-2.5-flash the miss is not marginal:
    two probe calls reported completion=10/reasoning=27 and completion=8/
    reasoning=66, i.e. we would record 11% of the real output on the second.

    So rather than making the operator declare the dialect per backend (a
    config knob nobody can verify, silently wrong when a backend changes), we
    let the response prove it: only when `total_tokens` exactly equals
    prompt + completion + reasoning is reasoning genuinely additional.

    Anything that does not add up — missing total, a backend that rounds, a
    dialect we have not seen — falls through to the OpenAI reading, which is
    both today's behaviour and the conservative one (it can only under-report,
    never invent a charge that was not on the bill).
    """
    if reasoning <= 0:
        return completion
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        return completion
    if int(total) == prompt + completion + reasoning:
        return completion + reasoning
    return completion


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

    cdetails = usage.get("completion_tokens_details")
    reasoning = 0
    if isinstance(cdetails, dict) and cdetails.get("reasoning_tokens") is not None:
        reasoning = int(cdetails["reasoning_tokens"] or 0)

    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)

    return Usage(
        prompt_tokens=prompt,
        completion_tokens=_fold_reasoning(
            prompt, completion, reasoning, usage.get("total_tokens")
        ),
        cached_tokens=cached,
        reasoning_tokens=reasoning,
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
