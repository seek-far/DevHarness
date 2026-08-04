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

    # WHICH COORDINATES A TRACE NEEDS IS THE PROVIDER'S KNOWLEDGE, NOT THIS
    # NODE'S. GitLab needs project_id + job_id (and returns "" without them,
    # rather than building a `.../jobs//trace` URL that 400s); the local
    # providers need neither — they read `--trace-file` or run `--test-cmd`.
    #
    # This node used to short-circuit on empty project_id/job_id and return ""
    # without ever calling the provider. That silently broke BOTH the standalone
    # CLI and every evaluation sweep (2026-07-26..2026-08-04): those paths run
    # LocalNoGitProvider, whose fetch_trace is the only place `--trace-file` /
    # `--test-cmd` is consumed, and they never carry a project_id/job_id — so
    # the trace was always empty and parse_trace aborted the run with
    # "trace is empty — nothing to analyse". The short-circuit's comment claimed
    # ver==0 "degrades gracefully into parse_trace's fallback path", but that
    # fallback is for a NON-empty trace the parser can't structure; an empty one
    # raises. It went unnoticed because the two paths exercised since then —
    # GitLab mode (always has both ids) and ver99 (routes to mini_react_loop
    # before parse_trace) — are exactly the two that skip this branch.
    trace, retries = with_transient_retry(
        lambda: provider.fetch_trace(project_id=project_id, job_id=job_id),
        op_name="fetch_trace",
    )
    logger.info("trace fetched (%d chars, retries=%d)", len(trace), retries)
    return {"trace": trace, "fetch_trace_retries": retries}
