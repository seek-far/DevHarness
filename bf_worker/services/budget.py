"""
services/budget.py

Per-``agent.fix()`` cost budget.

A hijacked or pathological run can in principle drive the LLM into a long
expensive loop (many fetches, many retries). The MAX_STEPS=8 cap inside one
``react_loop`` invocation is a per-loop fence; this module provides the
**per-run** fence across all react_loop invocations.

Three orthogonal dimensions are tracked, each with its own default; the
first to trip ends the run:

  - ``max_calls``       — total LLM calls inside this fix() (default 30)
  - ``max_tokens``      — total prompt+completion tokens (default 200_000)
  - ``max_wallclock_s`` — wall-clock seconds since fix() started (default 300)

Public API:
    RunBudget    — stateful budget object stored in state["budget"]
    BudgetConfig — frozen-defaults snapshot (for tests / introspection)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Honest single-bug runs typically use 2–8 LLM calls, 5–20k tokens, well
# under one minute. Defaults are set comfortably above that so they never
# trip on legitimate work, only on pathological / hijacked runs.
DEFAULT_MAX_CALLS = 30
DEFAULT_MAX_TOKENS = 200_000
DEFAULT_MAX_WALLCLOCK_S = 300
# Money is the one dimension where the honest default is "no opinion". Tokens
# and wallclock are comparable across backends; a dollar is not — the same run
# costs $0 self-hosted and real money on a cloud model. So the cap is OFF unless
# the operator names a number (BF_MAX_COST_USD). When it IS set, it is the only
# cap that bounds *spend* rather than *work*: the token cap can't, because the
# price per token varies by two orders of magnitude across backends.
DEFAULT_MAX_COST_USD: float | None = None


@dataclass
class RunBudget:
    """Tracks consumption against four caps. Mutable; one per fix() call."""

    max_calls: int = DEFAULT_MAX_CALLS
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_wallclock_s: int = DEFAULT_MAX_WALLCLOCK_S
    max_cost_usd: float | None = DEFAULT_MAX_COST_USD

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # None until a priced call lands. Distinct from 0.0, which would claim the
    # run was free — see cost_usd's docstring.
    cost_usd: float | None = None
    started_at: float = field(default_factory=time.monotonic)
    exhausted_reason: str | None = None

    # ── readers ───────────────────────────────────────────────────────────────

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    def is_exhausted(self) -> bool:
        return self.check() is not None

    def check(self) -> str | None:
        """Return an exhaustion reason if any cap is reached, else None.

        Caches the first reason in ``exhausted_reason`` so the run record
        always shows what tripped first, even if other caps later cross.
        """
        if self.exhausted_reason is not None:
            return self.exhausted_reason

        if self.calls >= self.max_calls:
            self.exhausted_reason = (
                f"call limit reached ({self.calls}/{self.max_calls})"
            )
        elif self.total_tokens >= self.max_tokens:
            self.exhausted_reason = (
                f"token limit reached ({self.total_tokens}/{self.max_tokens})"
            )
        elif (
            self.max_cost_usd is not None
            and self.cost_usd is not None
            and self.cost_usd >= self.max_cost_usd
        ):
            self.exhausted_reason = (
                f"cost limit reached (${self.cost_usd:.4f}/${self.max_cost_usd:.4f})"
            )
        elif self.elapsed_s >= self.max_wallclock_s:
            self.exhausted_reason = (
                f"wallclock limit reached ({int(self.elapsed_s)}/{self.max_wallclock_s}s)"
            )
        return self.exhausted_reason

    # ── writers ───────────────────────────────────────────────────────────────

    def record_call(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float | None = None,
    ) -> None:
        """Debit the budget for one LLM call.

        `cost_usd` is None on unpriced backends (self-hosted, or a gateway
        backend with no `pricing:` block). The cost accumulator then stays None
        and the cost cap simply never trips — an unknown price must not be
        treated as free, nor as infinite.
        """
        self.calls += 1
        self.input_tokens += max(0, int(input_tokens or 0))
        self.output_tokens += max(0, int(output_tokens or 0))
        if cost_usd is not None:
            self.cost_usd = (self.cost_usd or 0.0) + max(0.0, float(cost_usd))

    # ── serialisation (for journal / RunRecord) ───────────────────────────────

    def to_dict(self) -> dict:
        return {
            "max_calls": self.max_calls,
            "max_tokens": self.max_tokens,
            "max_wallclock_s": self.max_wallclock_s,
            "max_cost_usd": self.max_cost_usd,
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": (
                round(self.cost_usd, 6) if self.cost_usd is not None else None
            ),
            "elapsed_s": round(self.elapsed_s, 3),
            "exhausted_reason": self.exhausted_reason,
        }


@dataclass(frozen=True)
class BudgetConfig:
    """Immutable snapshot of budget defaults. Useful in tests."""

    max_calls: int = DEFAULT_MAX_CALLS
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_wallclock_s: int = DEFAULT_MAX_WALLCLOCK_S
    max_cost_usd: float | None = DEFAULT_MAX_COST_USD


def extract_token_usage(assistant_msg) -> tuple[int, int]:
    """Best-effort extraction of (input_tokens, output_tokens) from a LangChain
    assistant message.

    LangChain ChatOpenAI populates ``usage_metadata`` (preferred) and/or
    ``response_metadata['token_usage']`` (older path). Returns ``(0, 0)`` if
    neither is present so the budget still tracks call count even when the
    backend doesn't surface usage.
    """
    meta = getattr(assistant_msg, "usage_metadata", None)
    if meta:
        return int(meta.get("input_tokens", 0) or 0), int(meta.get("output_tokens", 0) or 0)

    rmeta = getattr(assistant_msg, "response_metadata", None) or {}
    usage = rmeta.get("token_usage") or rmeta.get("usage") or {}
    if usage:
        in_tok = int(usage.get("prompt_tokens", 0) or 0)
        out_tok = int(usage.get("completion_tokens", 0) or 0)
        return in_tok, out_tok

    return 0, 0


def extract_cached_input_tokens(assistant_msg) -> int | None:
    """Best-effort extraction of cached input tokens from a LangChain message.

    Returns the cached-prompt token count when the backend reports prompt
    caching, else None. Distinct from 0 — None means "backend didn't report",
    0 means "reported but nothing was cached this call".

    Two shapes are probed in order:
      1. LangChain (>=0.3) usage_metadata.input_token_details.cache_read
      2. Raw OpenAI passthrough in response_metadata: prompt_tokens_details.
         cached_tokens under either token_usage or usage.

    Returning None for unknown shapes lets downstream code distinguish
    "backend doesn't support prompt caching" (most self-hosted today) from
    "backend reported zero cache hits". The aggregate metric only counts
    backends that surface this — others contribute None and are filtered out.
    """
    meta = getattr(assistant_msg, "usage_metadata", None) or {}
    details = meta.get("input_token_details") if isinstance(meta, dict) else None
    if isinstance(details, dict) and details.get("cache_read") is not None:
        return int(details["cache_read"] or 0)

    rmeta = getattr(assistant_msg, "response_metadata", None) or {}
    usage = rmeta.get("token_usage") or rmeta.get("usage") or {}
    pdet = usage.get("prompt_tokens_details") if isinstance(usage, dict) else None
    if isinstance(pdet, dict) and pdet.get("cached_tokens") is not None:
        return int(pdet["cached_tokens"] or 0)

    return None
