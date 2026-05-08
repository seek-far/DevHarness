"""
Node: commit_change
Commit (and optionally push) via the provider. Only reached when test_passed=True.

Wraps `provider.commit_and_push()` in the shared transient-retry helper so a
single push timeout / 503 / TCP reset doesn't lose a passing fix. Permanent
errors (auth, 4xx) propagate immediately. Exhausted transients also propagate
— the run terminates via the existing failure path. The retry counter is
captured for telemetry on every successful commit.
"""

from __future__ import annotations
import logging

from graph.state import BugFixState
from typing import Optional
from langchain_core.runnables import RunnableConfig
from services.runtime_context import get_provider
from services.transient_retry import with_transient_retry

logger = logging.getLogger(__name__)


def commit_change(state: BugFixState, config: Optional[RunnableConfig] = None) -> BugFixState:
    provider = get_provider(config)
    repo_path = provider.ensure_repo_ready(state["bug_id"])

    result, retries = with_transient_retry(
        lambda: provider.commit_and_push(
            repo_path,
            message=f"ci_agent: auto-fix bug {state['bug_id']}",
        ),
        op_name="commit_change",
    )
    logger.info("commit_change result=%s (retries=%d)", result, retries)
    return {
        "commit_result": result,
        "commit_status": result.get("status"),
        "commit_branch": result.get("branch") or state.get("fix_branch_name"),
        "commit_hash": result.get("commit"),
        "commit_change_retries": retries,
    }
