"""
Node: fetch_trace
Pulls the raw CI job trace text via the provider and stores it in state.
"""

from __future__ import annotations
import logging

from graph.state import BugFixState

logger = logging.getLogger(__name__)


def fetch_trace(state: BugFixState) -> BugFixState:
    provider = state["provider"]
    trace = provider.fetch_trace(
        project_id=state.get("project_id", ""),
        job_id=state.get("job_id", ""),
    )
    logger.info("trace fetched (%d chars)", len(trace))
    return {"trace": trace}
