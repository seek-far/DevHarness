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
    no_fix_retry_count: int         # retries due to react_loop exhaustion (MAX_STEPS or abort_fix). Tracked SEPARATELY from fix_retry_count because (a) different cap (1 vs MAX_FIX_RETRIES=2), (b) no retry_feedback block makes sense — apply_error/test_output are absent on this path. Gateway attempt header sums both counters so the policy sees a single "I have failed N times" signal.

    # ── react loop ────────────────────────────────────────────────────────────
    react_step_count: int           # number of LLM calls made inside the loop
    react_messages: list            # full conversation history for debug/replay
    react_tool_calls: list          # log of every tool call: [{step, tool, input}]
    react_confidence: str | None    # "high" | "medium" | "low"
    react_reasoning: str | None     # LLM's stated reasoning from submit_fix
    max_input_tokens: int | None    # largest prompt_tokens reported by the backend across every LLM call this run (react_loop + reflection); 0 when backend never returned usage; None when no LLM call ever happened
    # ── per-run LLM latency / cost telemetry (additive 2026-05-24) ───────────
    # All accumulate across react_loop + reflection. None when no LLM call ran;
    # 0 (for the int fields) is a real observation. total_cached_input_tokens
    # stays None when the backend never reports prompt caching (most self-
    # hosted today); a single reporting call promotes it from None to int.
    llm_call_count: int | None
    total_prompt_tokens: int | None
    total_completion_tokens: int | None
    total_cached_input_tokens: int | None
    total_llm_wallclock_s: float | None
    # ── llm_gateway routing telemetry (additive 2026-05-25) ──────────────────
    # Set by react_loop / reflection when the worker is talking to an SDLCMA
    # llm_gateway. Reflects the backend name reported on the last LLM call's
    # X-Sdlcma-Backend-Name response header. Carry-forward semantics: a later
    # call that doesn't carry the header (e.g. all-backends-exhausted) does
    # not erase a previously-observed value. None in direct-backend mode.
    llm_backend_name: str | None

    # ── enhancements (optional) ───────────────────────────────────────────────
    memory_hint: str | None         # injected by memory enhancement (PRE_REACT_LOOP)
    memory_matches_count: int | None  # number of memory entries surfaced
    reflection_note: str | None     # injected by reflection enhancement (POST_APPLY_TEST) — causal post-mortem of the last failed attempt, rendered first in the retry prompt
    reflection_count: int | None    # number of reflection post-mortems produced this run (≤ MAX_FIX_RETRIES)
    reflection_mode: str | None     # last reflection's lens: "apply" (deterministic patch-mechanics note, no LLM) | "test" (LLM causal post-mortem)
    # Phase-2 reviewer has a `mode` (config-selected): "shadow" (default —
    # records findings, NEVER affects the fix; decouples measurement from
    # intervention at zero fix_rate risk) or "acting" (explicit opt-in — a
    # would-escalate finding is fed back into react_loop for a bounded extra
    # fixer round). Findings/telemetry are recorded in BOTH modes.
    code_review_status: str | None  # None=disabled | clean | advisory | would_escalate (shadow) | escalated (acting, fed back) | rounds_exhausted (acting, cap hit) | skipped_budget | skipped_no_files | error
    code_review_finding_count: int | None  # findings the inspector returned
    code_review_would_escalate: bool | None  # True iff a high-severity AND high-confidence finding was present (it WOULD act in acting mode; recorded in shadow too)
    code_review_findings: list | None  # compact per-finding dicts for offline evaluation
    code_review_note: str | None    # acting mode only: advisory feedback fed into react_loop (rendered by _format_retry_feedback); None in shadow mode
    code_review_rounds: int | None  # acting mode only: independent fixer↔review round counter (bounds the code_review→react_loop loop; separate from fix_retry_count)

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
