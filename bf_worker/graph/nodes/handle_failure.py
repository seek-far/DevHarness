"""
Node: handle_failure
Terminal node for all failure paths.
Logs a structured summary; extend here for Slack/email notifications, etc.
"""

from __future__ import annotations
import logging

from graph.state import BugFixState

logger = logging.getLogger(__name__)


def handle_failure(state: BugFixState) -> BugFixState:
    logger.error(
        "BugFix FAILED  bug_id=%s  ci_status=%s  test_passed=%s  "
        "llm_retry=%s  fix_retry=%s  error=%s",
        state.get("bug_id"),
        state.get("ci_status"),
        state.get("test_passed"),
        state.get("llm_retry_count", 0),
        state.get("fix_retry_count", 0),
        state.get("error"),
    )

    if state.get("test_output"):
        logger.debug("last test output:\n%s", state["test_output"])

    # TODO: send Slack / webhook notification here

    return {"error": state.get("error") or "workflow terminated in handle_failure"}
