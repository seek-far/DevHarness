"""
runtime_context — thin accessors for things that live in LangGraph's
`config["configurable"]` rather than in the graph state.

Why these moved out of state:
  - `provider`, `hooks`, `budget` are not serializable (provider holds
    connection handles, hooks contain function references). LangGraph's
    checkpointer serializes the whole state at every node boundary, so
    keeping non-serializable objects in state would break checkpointing.
  - LangGraph's idiom: `state` is data that flows between nodes; `config`
    is run-scoped context that's injected once. Provider/hooks/budget are
    run-scoped — they don't change between nodes — so config is the right
    home regardless of checkpointing.

Each accessor raises a clear error if the resource is missing rather
than returning None, because every node needs the provider; missing it
is a wiring bug, not a runtime condition.
"""

from __future__ import annotations
from typing import Any

from langchain_core.runnables import RunnableConfig


def _config_value(config: RunnableConfig | None, key: str, *, required: bool):
    if not config:
        if required:
            raise RuntimeError(
                f"missing config['configurable']['{key}'] — node was invoked "
                f"without a runtime config (LangGraphAgent.fix passes one)"
            )
        return None
    cfg = config.get("configurable") or {}
    val = cfg.get(key)
    if val is None and required:
        raise RuntimeError(
            f"missing config['configurable']['{key}'] in runtime config"
        )
    return val


def get_provider(config: RunnableConfig | None) -> Any:
    """Return the provider — required for every node."""
    return _config_value(config, "provider", required=True)


def get_hooks(config: RunnableConfig | None) -> Any:
    """Return the HookRegistry, or None when no enhancements are wired.

    Hooks are optional: a vanilla agent runs with an empty registry.
    """
    return _config_value(config, "hooks", required=False)


def get_budget(config: RunnableConfig | None) -> Any:
    """Return the RunBudget, or None when budget enforcement is disabled.

    Budget is technically required in production (LangGraphAgent always
    creates one), but we keep this optional so unit tests of individual
    nodes that don't exercise the budget path don't have to construct one.
    """
    return _config_value(config, "budget", required=False)
