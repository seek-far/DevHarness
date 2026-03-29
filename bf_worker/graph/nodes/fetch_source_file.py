"""
Node: fetch_source_file
Fetches the suspect source file content from GitLab.
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


def fetch_source_file(state: BugFixState) -> BugFixState:
    repo_path = Path(cfg.repo_base_path) / state["bug_id"]
    repo = Repo(repo_path=str(repo_path), repo_url=state["project_web_url"])
    content = repo.gitlab_fetch_file(state["suspect_file_path"])
    logger.info("fetched source file: %s (%d chars)", state["suspect_file_path"], len(content))
    return {"source_file_content": content}
