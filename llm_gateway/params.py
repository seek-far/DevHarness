"""Per-backend request-parameter normalisation.

This is the payoff of routing through a gateway at all: the worker emits ONE
canonical request shape, and per-backend divergence is absorbed here. Without
it, every backend quirk would have to be branched on inside the worker's LLM
client — and the worker cannot even see which backend the policy will pick.

Today one profile matters:

    reasoning — o-series and the whole gpt-5 family REJECT `temperature`.
                Azure answers:
                    HTTP 400 unsupported_value  param=temperature
                    "does not support 0 with this model. Only the default (1)
                     value is supported."
                (verified live 2026-07-14 against gpt-5-mini). They also want
                `max_completion_tokens`, not `max_tokens`.

Dropping the parameter is right; pinning it to 1 would be accepted but buys
nothing and hides the incompatibility from anyone reading the config.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import BackendConfig

logger = logging.getLogger(__name__)

# Params reasoning models refuse outright.
_REASONING_UNSUPPORTED = ("temperature", "top_p", "presence_penalty", "frequency_penalty")

# Params reasoning models renamed. old -> new.
_REASONING_RENAMED = {"max_tokens": "max_completion_tokens"}


def apply_param_profile(body: dict[str, Any], backend: BackendConfig) -> dict[str, Any]:
    """Rewrite `body` in place for `backend`'s parameter profile. Returns it.

    `chat` (the default, and every pre-existing config) is a strict no-op, so
    Dashscope / vLLM / any OpenAI-compatible backend keeps behaving exactly as
    before.
    """
    if backend.param_profile != "reasoning":
        return body

    dropped = [p for p in _REASONING_UNSUPPORTED if p in body]
    for p in dropped:
        body.pop(p, None)

    renamed = []
    for old, new in _REASONING_RENAMED.items():
        if old in body:
            # Don't clobber an explicit new-style value if the caller sent both.
            body.setdefault(new, body[old])
            body.pop(old, None)
            renamed.append(f"{old}->{new}")

    if backend.reasoning_effort:
        body.setdefault("reasoning_effort", backend.reasoning_effort)

    if dropped or renamed:
        logger.debug(
            "gateway: param_profile=reasoning backend=%s dropped=%s renamed=%s",
            backend.name, dropped or "-", renamed or "-",
        )
    return body
