"""
routing.py — all conditional_edge functions for the BugFix graph.

Each function receives the current BugFixState and returns a node-name string
that LangGraph will route to next.
"""

from __future__ import annotations
from graph.state import BugFixState

# ── tuneable limits ────────────────────────────────────────────────────────────
MAX_FIX_RETRIES = 2      # test-failure → re-ask retries (kept for routing compat;
                         # not triggered in practice since local tests are disabled)


# ── after react_loop ───────────────────────────────────────────────────────────

def route_after_react_loop(state: BugFixState) -> str:
    """
    llm_result is set  → create_fix_branch  (or apply directly if branch exists)
    llm_result is None → handle_failure
    """
    if state.get("llm_result") is not None:
        if state.get("fix_branch_name"):
            return "apply_change_and_test"   # branch-reuse path
        return "create_fix_branch"

    return "handle_failure"


# ── after apply_change_and_test ────────────────────────────────────────────────

def route_after_apply_and_test(state: BugFixState) -> str:
    """
    Passed   → commit_change
    Failed   → react_loop (retry with test_output)  or  handle_failure

"""
    if state.get("test_passed"):
        return "commit_change"

    if state.get("fix_retry_count", 0) < MAX_FIX_RETRIES:
        return "react_loop"

    return "handle_failure"


# ── after wait_ci_result ───────────────────────────────────────────────────────

def route_after_ci(state: BugFixState) -> str:
    if state.get("ci_status") == "success":
        return "create_mr"
    return "handle_failure"
