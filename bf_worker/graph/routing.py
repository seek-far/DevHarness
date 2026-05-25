"""
routing.py — all conditional_edge functions for the BugFix graph.

Each function receives the current BugFixState and returns a node-name string
that LangGraph will route to next.
"""

from __future__ import annotations
from graph.state import BugFixState
from settings import worker_cfg as cfg

# ── tuneable limits ────────────────────────────────────────────────────────────
MAX_FIX_RETRIES = 2      # test-failure → re-ask retries. On each retry the
                         # previous patch + truncated test_output + apply_error
                         # are injected into the LLM's prompt by
                         # react_loop._format_retry_feedback so it can revise
                         # rather than blindly resample.
NO_FIX_MAX_RETRIES = 1   # react_loop-exhaustion → re-ask retries. Independent
                         # cap from MAX_FIX_RETRIES because the failure mode is
                         # different: there is no apply_error / test_output to
                         # feed back, so the only meaningful action is "ask a
                         # DIFFERENT backend with the same prompt". Gated on
                         # cfg.llm_via_gateway — in direct-backend mode a re-
                         # entry just samples the same model again (pure waste),
                         # so we only retry when a gateway is in the path and
                         # the policy can actually advance. Cap=1 means "give
                         # the next backend exactly one shot", bounding the
                         # worst-case wasted spend.


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
    llm_result is None:
      - llm_via_gateway=True AND no_fix_retry_count ≤ NO_FIX_MAX_RETRIES
            → react_loop  (re-enter with attempt+1; gateway switches backend)
      - otherwise → handle_failure

    The bumping of no_fix_retry_count itself happens inside react_loop's
    return value — by the time this routing function runs, the counter
    already reflects this exit. So `<= NO_FIX_MAX_RETRIES` means "the count
    AFTER this just-finished exit is still within budget for another try".
    """
    if state.get("llm_result") is not None:
        if state.get("fix_branch_name"):
            return "apply_change_and_test"   # branch-reuse path
        return "create_fix_branch"

    if (
        getattr(cfg, "llm_via_gateway", False)
        and int(state.get("no_fix_retry_count") or 0) <= NO_FIX_MAX_RETRIES
    ):
        return "react_loop"  # self-loop for cross-backend retry

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
    Passed   → code_review (Phase-2 reviewer of the green patch; in shadow
               mode / disabled it only records and falls through to
               commit_change — see route_after_code_review)
    Failed   → react_loop (retry with test_output)  or  handle_failure
    """
    if state.get("test_passed"):
        return "code_review"

    if state.get("fix_retry_count", 0) < MAX_FIX_RETRIES:
        return "react_loop"

    return "handle_failure"


# ── after code_review ──────────────────────────────────────────────────────────

def route_after_code_review(state: BugFixState) -> str:
    """
    Only `escalated` branches back to react_loop — and ONLY the acting mode
    ever sets that status (within its bounded round cap). Shadow mode,
    disabled, clean, advisory, would_escalate, rounds_exhausted, skipped,
    error → commit_change: the reviewer must never block shipping a
    test-passing patch, and in shadow mode it can never branch at all.
    """
    if state.get("code_review_status") == "escalated":
        return "react_loop"
    return "commit_change"


# ── after wait_ci_result ───────────────────────────────────────────────────────

def route_after_ci(state: BugFixState) -> str:
    if state.get("ci_status") == "success":
        return "create_mr"
    return "handle_failure"
