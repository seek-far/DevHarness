"""
Node: create_mr
Creates a GitLab Merge Request from the fix branch into main.
"""

from __future__ import annotations
import logging
import sys
from pathlib import Path

from graph.state import BugFixState
from services.gitlab_utils import Repo

sys.path.append(str(Path.cwd().parent))
from settings import worker_cfg as cfg

logger = logging.getLogger(__name__)


def create_mr(state: BugFixState) -> BugFixState:
    repo_path = Path(cfg.repo_base_path) / state["bug_id"]
    repo = Repo(repo_path=str(repo_path), repo_url=state["project_web_url"])
    result = repo.gitlab_create_merge_request(
        source_branch=state["fix_branch_name"],
        title=f"[auto-fix] bug {state['bug_id']}",
        description=(
            f"Automatically generated fix for bug `{state['bug_id']}`.\n\n"
            f"**Error:**\n```\n{state.get('error_info', '')}\n```"
        ),
    )
    logger.info("MR created: !%s  %s", result["iid"], result["url"])
    return {}
