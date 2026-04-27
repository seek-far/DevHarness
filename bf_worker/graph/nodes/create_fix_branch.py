"""
Node: create_fix_branch

If state already has a fix_branch_name, reuse it (checkout + restore working tree).
Otherwise, ask the provider to prepare the repo and create a fresh branch.
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

    # Ensure repo/working directory is ready
    repo_path = provider.ensure_repo_ready(bug_id)

    if existing_branch:
        # Reuse existing branch — restore working tree
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
            # Non-git provider or branch doesn't exist — just continue
            pass
        logger.info("working tree restored on branch %s", existing_branch)
        return {}

    # Fresh branch
    logger.info("creating new fix branch for bug_id=%s", bug_id)
    result = provider.create_fix_branch(bug_id, repo_path)
    if result is None:
        raise RuntimeError("create_fix_branch returned None")

    logger.info("fix branch created: %s", result["branch_name"])
    return {"fix_branch_name": result["branch_name"]}
