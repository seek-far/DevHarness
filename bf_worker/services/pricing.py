"""What one LLM call cost, in direct-to-backend mode.

Two sources of truth, and the precedence matters:

  1. **Gateway mode** — the gateway is authoritative and returns the cost in the
     `X-Sdlcma-Cost-Usd` response header. It is the only component that knows
     which backend the policy actually picked and what that backend charges, and
     in a fallback ladder a single run can touch two backends at two prices.
  2. **Direct mode** — no gateway, so the worker prices the call itself from
     `configs/pricing.yaml`, keyed by `LLM_MODEL`.

An unknown model yields **None**, never a guess. A wrong number in a cost
dashboard is worse than a blank one: a blank prompts you to go look, a wrong
number gets quoted.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_PRICING_PATH = (
    Path(__file__).resolve().parents[2] / "configs" / "pricing.yaml"
)

_TABLE: dict[str, "ModelPrice"] | None = None


@dataclass(frozen=True)
class ModelPrice:
    input_per_1m: float = 0.0
    output_per_1m: float = 0.0
    cached_input_per_1m: float | None = None
    currency: str = "USD"

    def cost_usd(self, prompt: int, completion: int, cached: int = 0) -> float:
        """`cached` is the subset of `prompt` served from the provider's prompt
        cache — billed cheaper, so it is subtracted from the full-price portion
        rather than added on top."""
        cached = max(0, min(int(cached or 0), int(prompt or 0)))
        full = max(0, int(prompt or 0) - cached)
        cached_rate = (
            self.cached_input_per_1m
            if self.cached_input_per_1m is not None
            else self.input_per_1m
        )
        return (
            full * self.input_per_1m / 1_000_000
            + cached * cached_rate / 1_000_000
            + max(0, int(completion or 0)) * self.output_per_1m / 1_000_000
        )


def _coerce(raw: Any) -> ModelPrice | None:
    if not isinstance(raw, dict):
        return None
    try:
        return ModelPrice(
            input_per_1m=float(raw.get("input_per_1m", 0) or 0),
            output_per_1m=float(raw.get("output_per_1m", 0) or 0),
            cached_input_per_1m=(
                float(raw["cached_input_per_1m"])
                if raw.get("cached_input_per_1m") is not None
                else None
            ),
            currency=str(raw.get("currency", "USD")),
        )
    except (TypeError, ValueError):
        return None


def load_table(path: str | Path | None = None) -> dict[str, ModelPrice]:
    """Load (and memoise) the model→price table. A missing/unparseable file is
    not fatal — the run proceeds with cost unknown."""
    global _TABLE
    if _TABLE is not None and path is None:
        return _TABLE

    p = Path(path or os.environ.get("BF_PRICING_FILE") or _DEFAULT_PRICING_PATH)
    table: dict[str, ModelPrice] = {}
    if p.is_file():
        try:
            import yaml

            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            for model, raw in (data.get("models") or {}).items():
                price = _coerce(raw)
                if price is not None:
                    table[str(model)] = price
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("pricing: could not read %s: %s (costs will be None)", p, exc)
    else:
        logger.debug("pricing: no table at %s; direct-mode costs will be None", p)

    if path is None:
        _TABLE = table
    return table


def reset_cache() -> None:
    """Drop the memoised table. For tests."""
    global _TABLE
    _TABLE = None


def cost_for(
    model: str, prompt: int, completion: int, cached: int = 0
) -> float | None:
    """Cost of one call in direct mode, or None when the model isn't priced."""
    if not model:
        return None
    price = load_table().get(model)
    if price is None:
        return None
    return price.cost_usd(prompt, completion, cached)
