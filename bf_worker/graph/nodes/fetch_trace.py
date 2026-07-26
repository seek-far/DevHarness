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

    # No job to fetch a trace from → return an empty trace instead of building
    # a `.../jobs//trace` URL that 400s. The real webhook flow always carries a
    # failed job_id; an empty one only arises when a caller has no CI job to
    # point at (e.g. the workflow_ver==99 SWE-bench substrate, which never uses
    # the trace — mini works from problem_statement, not the CI trace). ver==0
    # degrades gracefully: parse_trace sees an empty trace and takes its
    # fallback path into react_loop.
    if not project_id or not job_id:
        logger.info("fetch_trace: no project_id/job_id (project=%r job=%r) — skipping, empty trace",
                    project_id, job_id)
        return {"trace": "", "fetch_trace_retries": 0}

    trace, retries = with_transient_retry(
        lambda: provider.fetch_trace(project_id=project_id, job_id=job_id),
        op_name="fetch_trace",
    )
    logger.info("trace fetched (%d chars, retries=%d)", len(trace), retries)
    return {"trace": trace, "fetch_trace_retries": retries}
