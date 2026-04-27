"""
Node: wait_ci_result

Delegates to the provider's wait_ci_result method.
For GitLab: blocks on Redis inbox stream for CI pipeline result.
For local: returns success immediately (tests already ran locally).
"""

from __future__ import annotations
import logging

from graph.state import BugFixState

logger = logging.getLogger(__name__)


def wait_ci_result(state: BugFixState, timeout: int = 300) -> BugFixState:
    provider = state["provider"]
    logger.info("waiting for CI result (timeout=%ds) bug=%s", timeout, state["bug_id"])
    status = provider.wait_ci_result(state["bug_id"], timeout)

    if status is None:
        logger.warning("CI wait timed out")
        return {"ci_status": "timeout"}

    logger.info("CI result: %s", status)
    return {"ci_status": status}
