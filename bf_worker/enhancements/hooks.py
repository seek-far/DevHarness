"""
HookRegistry — named extension points that LangGraphAgent callbacks register against.

Design:
  - One registry instance per agent run, attached to BugFixState["hooks"].
  - Each enhancement (memory, multi-hypothesis, edge-case-tests, ...) registers
    one or more callbacks at named hook points.
  - The core graph nodes invoke `state["hooks"].run(name, state)` at extension
    points; the call is a no-op when no callbacks are registered, so the
    baseline (no enhancements) behaves identically to the pre-hook code.
  - Callbacks may return a dict to merge into state, or None to leave state alone.
  - Exceptions in callbacks are logged and swallowed — an enhancement should
    never break the core run.

Hook naming convention: <when>_<what>, e.g. pre_react_loop, post_apply_test.

Note on placement: agent-boundary hooks (AGENT_PRE_FIX, AGENT_POST_FIX) are
called from LangGraphAgent.fix() and are wired in today. Graph-internal hooks
(PRE_REACT_LOOP, etc.) are *named* here so future enhancements know what to
register against, but call sites inside graph nodes are added when the first
enhancement needs them — adding hook calls without a concrete consumer would
be premature.
"""

from __future__ import annotations
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


class HookName:
    """Named extension points. Strings, namespaced for easy grep."""

    # Agent boundary — wired in LangGraphAgent.fix()
    AGENT_PRE_FIX        = "agent.pre_fix"        # state -> state | None
    AGENT_POST_FIX       = "agent.post_fix"       # state -> state | None

    # Graph-internal — declared but not yet wired (see module docstring).
    # When implementing the first enhancement that needs one of these,
    # add the call site to the corresponding node and remove this notice.
    PRE_REACT_LOOP       = "graph.pre_react_loop"
    POST_REACT_LOOP      = "graph.post_react_loop"
    PRE_APPLY_TEST       = "graph.pre_apply_test"
    POST_APPLY_TEST      = "graph.post_apply_test"


HookCallback = Callable[[dict], dict | None]


class HookRegistry:
    def __init__(self) -> None:
        self._hooks: dict[str, list[HookCallback]] = {}

    def register(self, hook_name: str, fn: HookCallback) -> None:
        """Register a callback at a named hook point.

        The callback receives the current state and returns either:
          - a dict whose keys are merged into state, or
          - None, leaving state untouched.
        """
        self._hooks.setdefault(hook_name, []).append(fn)
        logger.debug("hook registered: %s -> %s", hook_name, getattr(fn, "__name__", fn))

    def has(self, hook_name: str) -> bool:
        return bool(self._hooks.get(hook_name))

    def run(self, hook_name: str, state: dict) -> dict:
        """Invoke every callback for `hook_name`, merging dict returns into state."""
        callbacks = self._hooks.get(hook_name, [])
        if not callbacks:
            return state
        for fn in callbacks:
            try:
                update = fn(state)
            except Exception as exc:
                logger.warning("hook %s callback %s raised: %s",
                               hook_name, getattr(fn, "__name__", fn), exc)
                continue
            if update:
                state = {**state, **update}
        return state

    def names(self) -> list[str]:
        return list(self._hooks.keys())

    def __repr__(self) -> str:
        counts = {k: len(v) for k, v in self._hooks.items()}
        return f"HookRegistry({counts})"
