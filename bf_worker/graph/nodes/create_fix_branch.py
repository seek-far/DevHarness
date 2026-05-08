"""
Node: create_fix_branch

Two responsibilities:
  1. Resolve the deterministic fix branch (create or reuse — provider does the work).
  2. Surface any existing MR for that branch so the graph can short-circuit
     when the fix has already been merged (R10).

If state already carries a fix_branch_name (LangGraph re-entry on retry), we
just restore the working tree on that branch and proceed.
"""

from __future__ import annotations
import logging
import subprocess
from pathlib import Path

from graph.state import BugFixState

logger = logging.getLogger(__name__)


def create_fix_branch(state: BugFixState) -> BugFixState:
    provider = state["provider"]
    bug_id = state["bug_id"]
    existing_branch = state.get("fix_branch_name")

    repo_path = provider.ensure_repo_ready(bug_id)

    if existing_branch:
        logger.info("reusing existing fix branch: %s", existing_branch)
        try:
            subprocess.run(
                ["git", "checkout", existing_branch],
                cwd=str(repo_path), capture_output=True, text=True, check=True,
            )
            subprocess.run(
                ["git", "checkout", "--", "."],
                cwd=str(repo_path), capture_output=True, text=True, check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        logger.info("working tree restored on branch %s", existing_branch)
        return {}

    logger.info("creating fix branch for bug_id=%s", bug_id)
    result = provider.create_fix_branch(bug_id, repo_path)
    if result is None:
        raise RuntimeError("create_fix_branch returned None")

    update: BugFixState = {
        "fix_branch_name":      result["branch_name"],
        "branch_create_result": result,
        "branch_create_status": result.get("status"),
        "base_branch":          result.get("base_branch"),
        "base_commit":          result.get("commit"),
    }

    # R10 short-circuit: if the deterministic branch already has a merged MR,
    # the fix is shipped — don't redo apply/commit/push/MR. Surface the merged
    # MR's metadata into the state slots commit_change/create_mr would have
    # written, so RunRecord telemetry stays consistent.
    existing_mr = result.get("existing_mr") if isinstance(result, dict) else None
    if existing_mr and existing_mr.get("state") == "merged":
        logger.info("R10 short-circuit: MR %s already merged; skipping apply/commit/MR",
                    existing_mr.get("url"))
        update["already_fixed"]  = True
        update["review_result"]  = existing_mr
        update["review_status"]  = "already_merged"
        update["review_url"]     = existing_mr.get("url")
        update["review_id"]      = existing_mr.get("id")
        update["review_iid"]     = existing_mr.get("iid")
        update["review_branch"]  = result["branch_name"]

    logger.info("fix branch ready: %s (status=%s)",
                result["branch_name"], result.get("status"))
    return update
