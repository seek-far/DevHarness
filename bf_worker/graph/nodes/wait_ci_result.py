"""
Node: wait_ci_result

Delegates to the provider's wait_ci_result method.
For GitLab: blocks on Redis inbox stream for CI pipeline result.
For local: returns success immediately (tests already ran locally).

Wrapped in the shared transient-retry helper so a Redis disconnect mid-wait
or a transient connection error retries instead of killing the run.
A `None` return is *not* a transient — it's a deliberate "timeout reached"
signal from the provider — and is passed through unchanged. Each retry is
an independent call; the per-attempt timeout is not reduced, so a worst-case
sequence of two transient drops with the default 300s timeout could spend
up to 900s before either succeeding or giving up.
"""

from __future__ import annotations
import logging
import time

from graph.state import BugFixState
from typing import Optional
from langchain_core.runnables import RunnableConfig
from services.runtime_context import get_provider
from services.transient_retry import with_transient_retry

logger = logging.getLogger(__name__)

_DEFAULT_CI_TIMEOUT_S = 300


def wait_ci_result(state: BugFixState, config: Optional[RunnableConfig] = None) -> BugFixState:
    provider = get_provider(config)
    timeout = _DEFAULT_CI_TIMEOUT_S
    bug_id = state["bug_id"]
    logger.info("waiting for CI result (timeout=%ds) bug=%s", timeout, bug_id)

    # Phase-3 sub-marker: brackets the "we pushed the fix branch, now we're
    # blocking on the orchestrator routing a ValidationStatusEvent back to
    # us" interval. This is where the wallclock cost depends on GitLab's
    # CI scheduler, not on the worker — so under burst this number ramps
    # with GitLab runner contention even when the worker itself is idle.
    _t0_ms = time.time_ns() // 1_000_000
    logger.info(
        "phase_marker phase=ci_wait_start bug_id=%s t_wall_ms=%d",
        bug_id, _t0_ms,
    )

    status, retries = with_transient_retry(
        lambda: provider.wait_ci_result(bug_id, timeout),
        op_name="wait_ci_result",
    )

    _t1_ms = time.time_ns() // 1_000_000
    logger.info(
        "phase_marker phase=ci_wait_end bug_id=%s ci_status=%s retries=%d "
        "elapsed_ms=%d t_wall_ms=%d",
        bug_id, (status if status is not None else "timeout"),
        retries, _t1_ms - _t0_ms, _t1_ms,
    )

    if status is None:
        logger.warning("CI wait timed out (retries=%d)", retries)
        return {"ci_status": "timeout", "wait_ci_result_retries": retries}

    logger.info("CI result: %s (retries=%d)", status, retries)
    return {"ci_status": status, "wait_ci_result_retries": retries}
