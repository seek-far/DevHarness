"""
BugFixState — the single source of truth passed between all LangGraph nodes.
"""

from __future__ import annotations
from typing import Any, TypedDict


class BugFixState(TypedDict, total=False):
    # NOTE: `provider`, `hooks`, and `budget` are intentionally NOT in state.
    # They live in `config["configurable"]` (see services.runtime_context),
    # because (a) state is checkpoint-serialized at every node boundary and
    # those objects don't pickle, and (b) LangGraph's idiom is "state flows
    # between nodes, config is run-scoped context". Nodes access them via
    # `get_provider(config)` / `get_hooks(config)` / `get_budget(config)`.
    # The journal sees a `budget` key (dict snapshot) injected by
    # LangGraphAgent.fix() *after* the graph completes, for telemetry only.

    # ── inputs ────────────────────────────────────────────────────────────────
    bug_id: str
    project_id: str
    project_web_url: str
    job_id: str

    # ── trace / parse ─────────────────────────────────────────────────────────
    trace: str
    error_info: str
    suspect_file_path: str          # e.g. "api/views.py"; "" when parse_trace_fallback is True
    source_file_content: str
    parse_trace_fallback: bool      # True when parser produced no structured info — error_info holds the raw trace tail and suspect_file_path is ""
    source_fetch_failed: bool       # True when parser produced a path but provider.fetch_file raised — suspect_file_path kept for telemetry, source_file_content is ""
    fetch_trace_retries: int        # number of transient-retry loops fetch_trace took before succeeding (0 = first-attempt success)
    fetch_source_file_retries: int  # transient retries for provider.fetch_file in fetch_source_file (only when the fetch eventually succeeded)

    # ── llm ───────────────────────────────────────────────────────────────────
    llm_result: dict | None         # full JSON: {can_fix, error_reason, step_by_step_thinking, fixes}
    fix_retry_count: int            # retries due to test failure (kept for routing compat)

    # ── react loop ────────────────────────────────────────────────────────────
    react_step_count: int           # number of LLM calls made inside the loop
    react_messages: list            # full conversation history for debug/replay
    react_tool_calls: list          # log of every tool call: [{step, tool, input}]
    react_confidence: str | None    # "high" | "medium" | "low"
    react_reasoning: str | None     # LLM's stated reasoning from submit_fix

    # ── enhancements (optional) ───────────────────────────────────────────────
    memory_hint: str | None         # injected by memory enhancement (PRE_REACT_LOOP)
    memory_matches_count: int | None  # number of memory entries surfaced

    # ── branch / apply ────────────────────────────────────────────────────────
    fix_branch_name: str | None
    branch_create_result: dict | None
    branch_create_status: str | None
    base_branch: str | None
    base_commit: str | None
    commit_result: dict | None
    commit_status: str | None
    commit_branch: str | None
    commit_hash: str | None
    review_result: dict | None
    review_status: str | None
    review_url: str | None
    review_id: int | str | None
    review_iid: int | str | None
    review_branch: str | None
    patch_file: str | None
    report_file: str | None
    apply_error: str | None         # non-None when apply_patch itself crashed
    commit_change_retries: int      # transient retries for provider.commit_and_push in commit_change
    create_mr_retries: int          # transient retries for provider.create_review in create_mr

    # ── test ──────────────────────────────────────────────────────────────────
    test_passed: bool | None
    test_output: str | None         # full pytest stdout+stderr

    # ── ci / mr ───────────────────────────────────────────────────────────────
    ci_status: str | None           # "success" | "failed" | "timeout"
    wait_ci_result_retries: int     # transient retries for provider.wait_ci_result in wait_ci_result (0 = first-attempt return; status=timeout still records 0)

    # ── idempotency / R10 short-circuit ───────────────────────────────────────
    already_fixed: bool             # True when create_fix_branch saw a merged MR for the deterministic branch
                                    # — apply/commit/MR are skipped; review_status is set to "already_merged"

    # ── generic error slot ────────────────────────────────────────────────────
    error: str | None
