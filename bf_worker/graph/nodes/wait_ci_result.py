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
import os
import time

from graph.state import BugFixState
from typing import Optional
from langchain_core.runnables import RunnableConfig
from services.runtime_context import get_provider
from services.transient_retry import with_transient_retry

logger = logging.getLogger(__name__)

_DEFAULT_CI_TIMEOUT_S = 300


def _ci_timeout() -> int:
    """CI-wait timeout (seconds). Default 300 (byte-identical to before). Override
    with BF_CI_WAIT_TIMEOUT — needed for slow validation pipelines: SWE-bench
    ver==99 CI recompiles C-extension repos (astropy `pip install -e .` ≈ 2-4 min)
    and, under concurrent sweeps, queues behind other jobs on the runner, so the
    real auto/bf pipeline can exceed 5 min and the worker would otherwise time out
    while the CI is still (successfully) running."""
    raw = os.environ.get("BF_CI_WAIT_TIMEOUT", "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            logger.warning("BF_CI_WAIT_TIMEOUT=%r not a positive int; using default", raw)
    return _DEFAULT_CI_TIMEOUT_S


def wait_ci_result(state: BugFixState, config: Optional[RunnableConfig] = None) -> BugFixState:
    provider = get_provider(config)
    timeout = _ci_timeout()
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

    ver99 = int(state.get("workflow_ver") or 0) == 99

    if status is None:
        logger.warning("CI wait timed out (retries=%d)", retries)
        out = {"ci_status": "timeout", "wait_ci_result_retries": retries}
        # workflow_ver == 99: CI IS the SWE-bench oracle, so resolved mirrors it.
        # A timeout is not a resolve.
        if ver99:
            out["resolved"] = False
        return out

    logger.info("CI result: %s (retries=%d)", status, retries)
    out = {"ci_status": status, "wait_ci_result_retries": retries}
    if ver99:
        # resolved iff FAIL_TO_PASS+PASS_TO_PASS all pass, which on this path IS
        # the CI job's exit code (the .gitlab-ci.yml runs exactly those tests).
        out["resolved"] = (status == "success")
    return out
