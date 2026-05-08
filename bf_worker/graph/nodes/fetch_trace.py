"""
Node: fetch_trace
Pulls the raw CI job trace text via the provider and stores it in state.

Wraps `provider.fetch_trace()` in a narrow transient-retry loop (shared with
the four other I/O-bound graph nodes via `services.transient_retry`).
Permanent errors propagate immediately; the retry counter goes into state
for journal/RunRecord telemetry.
"""

from __future__ import annotations
import logging

from graph.state import BugFixState
from typing import Optional
from langchain_core.runnables import RunnableConfig
from services.runtime_context import get_provider
from services.transient_retry import with_transient_retry

logger = logging.getLogger(__name__)


def fetch_trace(state: BugFixState, config: Optional[RunnableConfig] = None) -> BugFixState:
    provider = get_provider(config)
    project_id = state.get("project_id", "")
    job_id = state.get("job_id", "")

    trace, retries = with_transient_retry(
        lambda: provider.fetch_trace(project_id=project_id, job_id=job_id),
        op_name="fetch_trace",
    )
    logger.info("trace fetched (%d chars, retries=%d)", len(trace), retries)
    return {"trace": trace, "fetch_trace_retries": retries}
