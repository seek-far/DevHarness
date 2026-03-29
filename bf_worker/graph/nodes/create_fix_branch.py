"""
Node: create_fix_branch

Method A (branch reuse):
  - If state already has a fix_branch_name the branch was created in a
    previous cycle.  We just checkout that branch and restore the working
    tree so the next apply starts from a clean slate.
  - Otherwise clone the repo, create a fresh branch, and push it.
"""

from __future__ import annotations
import logging
import os
import sys

from graph.state import BugFixState
from services.gitlab_utils import Repo

from pathlib import Path
sys.path.append(str(Path.cwd().parent))
from settings import worker_cfg as cfg

logger = logging.getLogger(__name__)


def create_fix_branch(state: BugFixState) -> BugFixState:
    repo_path = Path(cfg.repo_base_path) / state["bug_id"]
    repo = Repo(repo_path=str(repo_path), repo_url=state["project_web_url"])
    existing_branch = state.get("fix_branch_name")

    if existing_branch:
        # ── Method A: reuse existing branch ───────────────────────────────────
        logger.info("reusing existing fix branch: %s", existing_branch)
        # Repo dir should still exist from the previous cycle; if not, re-clone.
        if not (repo.repo_path / ".git").exists():
            logger.info("repo dir gone, re-cloning for branch reuse")
            repo.ensure_repo_ready()
            repo.run("checkout", existing_branch)
        else:
            repo.run("checkout", existing_branch)
        # Restore working tree to HEAD so the previous (failed) patch is gone.
        repo.run("checkout", "--", ".")
        logger.info("working tree restored to HEAD on branch %s", existing_branch)
        return {}   # no state change needed — branch name is already correct

    # ── Fresh branch ──────────────────────────────────────────────────────────
    logger.info("creating new fix branch for bug_id=%s", state["bug_id"])
    result = repo.create_fix_branch(bug_id=state["bug_id"])
    if result is None:
        raise RuntimeError("create_fix_branch returned None")

    logger.info("fix branch created: %s", result["branch_name"])
    return {"fix_branch_name": result["branch_name"]}
