"""
Node: commit_change
Git add + commit + push.  Only reached when test_passed=True.
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


def commit_change(state: BugFixState) -> BugFixState:
    repo_path = Path(cfg.repo_base_path) / state["bug_id"]
    repo = Repo(repo_path=str(repo_path), repo_url=state["project_web_url"])
    result = repo.commit_changes(
        message=f"ci_agent: auto-fix bug {state['bug_id']}"
    )
    logger.info("commit_change result=%s", result)
    return {}
