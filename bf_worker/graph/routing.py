"""
routing.py — all conditional_edge functions for the BugFix graph.

Each function receives the current BugFixState and returns a node-name string
that LangGraph will route to next.
"""

from __future__ import annotations
from graph.state import BugFixState

# ── tuneable limits ────────────────────────────────────────────────────────────
MAX_FIX_RETRIES = 2      # test-failure → re-ask retries. On each retry the
                         # previous patch + truncated test_output + apply_error
                         # are injected into the LLM's prompt by
                         # react_loop._format_retry_feedback so it can revise
                         # rather than blindly resample.


# ── after precheck_already_fixed ───────────────────────────────────────────────

def route_after_precheck(state: BugFixState) -> str:
    """
    R10 early short-circuit — if a merged MR was already found, exit.
    Otherwise proceed into the normal pipeline.
    """
    if state.get("already_fixed"):
        return "already_fixed"
    return "fetch_trace"


# ── after parse_trace ──────────────────────────────────────────────────────────

def route_after_parse_trace(state: BugFixState) -> str:
    """
    Suspect file identified → fetch_source_file (normal path).
    No suspect file → react_loop (fallback: LLM works from the raw trace and
    must use fetch_additional_file to explore).
    """
    if state.get("suspect_file_path"):
        return "fetch_source_file"
    return "react_loop"


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


# ── after create_fix_branch ────────────────────────────────────────────────────

def route_after_create_fix_branch(state: BugFixState) -> str:
    """
    R10 short-circuit: if create_fix_branch saw an already-merged MR for the
    deterministic branch, the fix is shipped — skip apply/commit/MR.

    Normal path → apply_change_and_test.
    """
    if state.get("already_fixed"):
        return "already_fixed"
    return "apply_change_and_test"


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
