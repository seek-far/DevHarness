"""
Node: commit_change
Commit (and optionally push) via the provider. Only reached when test_passed=True.
"""

from __future__ import annotations
import logging
from pathlib import Path

from graph.state import BugFixState

logger = logging.getLogger(__name__)


def commit_change(state: BugFixState) -> BugFixState:
    provider = state["provider"]
    repo_path = provider.ensure_repo_ready(state["bug_id"])
    result = provider.commit_and_push(
        repo_path,
        message=f"ci_agent: auto-fix bug {state['bug_id']}",
    )
    logger.info("commit_change result=%s", result)
    return {}
