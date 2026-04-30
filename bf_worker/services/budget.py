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


@dataclass
class RunBudget:
    """Tracks consumption against three caps. Mutable; one per fix() call."""

    max_calls: int = DEFAULT_MAX_CALLS
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_wallclock_s: int = DEFAULT_MAX_WALLCLOCK_S

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
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
        elif self.elapsed_s >= self.max_wallclock_s:
            self.exhausted_reason = (
                f"wallclock limit reached ({int(self.elapsed_s)}/{self.max_wallclock_s}s)"
            )
        return self.exhausted_reason

    # ── writers ───────────────────────────────────────────────────────────────

    def record_call(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        """Debit the budget for one LLM call."""
        self.calls += 1
        self.input_tokens += max(0, int(input_tokens or 0))
        self.output_tokens += max(0, int(output_tokens or 0))

    # ── serialisation (for journal / RunRecord) ───────────────────────────────

    def to_dict(self) -> dict:
        return {
            "max_calls": self.max_calls,
            "max_tokens": self.max_tokens,
            "max_wallclock_s": self.max_wallclock_s,
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "elapsed_s": round(self.elapsed_s, 3),
            "exhausted_reason": self.exhausted_reason,
        }


@dataclass(frozen=True)
class BudgetConfig:
    """Immutable snapshot of budget defaults. Useful in tests."""

    max_calls: int = DEFAULT_MAX_CALLS
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_wallclock_s: int = DEFAULT_MAX_WALLCLOCK_S


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
