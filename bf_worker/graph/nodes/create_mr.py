"""
Node: create_mr
Creates a review artifact via the provider.
For GitLab: creates a Merge Request.
For local: writes a report file and patch.
"""

from __future__ import annotations
import logging

from graph.state import BugFixState

logger = logging.getLogger(__name__)


def create_mr(state: BugFixState) -> BugFixState:
    provider = state["provider"]
    repo_path = provider.ensure_repo_ready(state["bug_id"])
    result = provider.create_review(repo_path, state)
    logger.info("create_review result=%s", result)
    return {
        "review_result": result,
        "review_status": result.get("status") or result.get("state"),
        "review_url": result.get("url"),
        "review_id": result.get("id"),
        "review_iid": result.get("iid"),
        "review_branch": result.get("branch") or state.get("commit_branch") or state.get("fix_branch_name"),
        "patch_file": result.get("patch_file"),
        "report_file": result.get("report_file"),
    }
