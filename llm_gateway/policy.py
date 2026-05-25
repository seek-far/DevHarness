"""Inference policy — stateless backend selection.

Today: one policy type, `ordered`. Selection is a pure function of
the worker's `X-Sdlcma-Attempt` header (the react_loop retry counter,
monotone within a bug) and the configured `policy.order`.

Selection rule for `ordered` (when `advance_on_fix_failure` is true):

    index = clamp(attempt, 0, len(order) - 1)

When `advance_on_fix_failure` is false the index is always 0 — single-backend
configs and "pin to primary" configs both degenerate to this.

**No per-bug state.** Earlier drafts kept a per-bug backend-error tally so
the gateway could mark a backend "dead for this bug" and skip it on the
same request. That bought near-zero value in practice: backend failures
are properties of the backend (overload / network / rate limit), not of
the bug — so per-bug isolation is theoretical. It cost a growing dict
(LRU-cappable but still complexity), a lock, and a richer test surface.
The simpler model: trust the gateway's narrow transient retry and the
worker's react_loop attempt counter. When a backend is genuinely down, the
first call wastes one transient-retry budget, the worker's next react_loop
entry comes with `attempt+1`, and the policy advances by itself. Cost is
one wasted call per outage; benefit is a stateless policy with O(N
backends) memory footprint.

Process-wide circuit breaking (count consecutive backend failures, open the
circuit for T seconds) is the right next layer if backend outages become a
real problem — but it lives outside the per-request policy, has bounded
state by design, and is the industry-standard pattern. Out of scope today.

Determinism: same `attempt` + same config → same backend. No randomness,
no time-based decay, no shared mutable state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .config import BackendConfig, GatewayConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Selection:
    backend: BackendConfig
    reason: str  # short tag for logs / response header: "primary" | "fix_failure_advance"


class InferencePolicy:
    """Stateless ordered-ladder policy.

    Kept as a class (not a free function) for parity with future policy types
    that may legitimately hold state (e.g. circuit-breaker would hold
    process-wide per-backend health gauges; cost-aware might hold a sliding
    window of token spend). Today's `ordered` policy has none — every method
    is effectively pure over (config, attempt).
    """

    def __init__(self, cfg: GatewayConfig) -> None:
        self._cfg = cfg

    @property
    def config(self) -> GatewayConfig:
        return self._cfg

    def select(self, attempt: int) -> Selection:
        """Pick a backend for an LLM call. Pure function of attempt + config.

        `attempt` is the worker's `X-Sdlcma-Attempt` header value (= the
        react_loop retry counter). Treated as a difficulty coefficient:
        attempt=0 → primary, attempt=1 → fallback[0], etc. Clamped to the
        last entry when attempt exceeds the configured order.
        """
        order = self._cfg.policy.order
        if self._cfg.policy.advance_on_fix_failure:
            idx = max(0, min(int(attempt), len(order) - 1))
        else:
            idx = 0
        reason = "primary" if idx == 0 else "fix_failure_advance"
        backend = self._cfg.backends_by_name[order[idx]]
        return Selection(backend=backend, reason=reason)


def parse_attempt_header(value: Optional[str]) -> int:
    """Parse `X-Sdlcma-Attempt` defensively. Missing / malformed → 0."""
    if not value:
        return 0
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, n)
