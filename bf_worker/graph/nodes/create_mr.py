"""
Node: create_mr
Creates a review artifact via the provider.
For GitLab: creates a Merge Request.
For local: writes a report file and patch.

Wrapped in the shared transient-retry helper so a transient GitLab API
failure (5xx, 429, connection reset) doesn't lose a successful fix at the
last hop. Permanent errors propagate immediately.
"""

from __future__ import annotations
import logging

from graph.state import BugFixState
from typing import Optional
from langchain_core.runnables import RunnableConfig
from services.runtime_context import get_provider
from services.transient_retry import with_transient_retry

logger = logging.getLogger(__name__)


def create_mr(state: BugFixState, config: Optional[RunnableConfig] = None) -> BugFixState:
    provider = get_provider(config)
    repo_path = provider.ensure_repo_ready(state["bug_id"])

    result, retries = with_transient_retry(
        lambda: provider.create_review(repo_path, state),
        op_name="create_mr",
    )
    logger.info("create_review result=%s (retries=%d)", result, retries)
    return {
        "review_result": result,
        "review_status": result.get("status") or result.get("state"),
        "review_url": result.get("url"),
        "review_id": result.get("id"),
        "review_iid": result.get("iid"),
        "review_branch": result.get("branch") or state.get("commit_branch") or state.get("fix_branch_name"),
        "patch_file": result.get("patch_file"),
        "report_file": result.get("report_file"),
        "create_mr_retries": retries,
    }
