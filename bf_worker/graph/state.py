"""
BugFixState — the single source of truth passed between all LangGraph nodes.
"""

from __future__ import annotations
from typing import Any, TypedDict


class BugFixState(TypedDict, total=False):
    # ── provider (injected at startup) ────────────────────────────────────────
    provider: Any  # SourceProvider & VCSProvider & ReviewProvider instance

    # ── hook registry (LangGraphAgent-only; absent for other agents) ──────────
    hooks: Any     # enhancements.hooks.HookRegistry — present only when enhancements are wired

    # ── inputs ────────────────────────────────────────────────────────────────
    bug_id: str
    project_id: str
    project_web_url: str
    job_id: str

    # ── trace / parse ─────────────────────────────────────────────────────────
    trace: str
    error_info: str
    suspect_file_path: str          # e.g. "api/views.py"
    source_file_content: str

    # ── llm ───────────────────────────────────────────────────────────────────
    llm_result: dict | None         # full JSON: {can_fix, error_reason, step_by_step_thinking, fixes}
    fix_retry_count: int            # retries due to test failure (kept for routing compat)

    # ── react loop ────────────────────────────────────────────────────────────
    react_step_count: int           # number of LLM calls made inside the loop
    react_messages: list            # full conversation history for debug/replay
    react_tool_calls: list          # log of every tool call: [{step, tool, input}]
    react_confidence: str | None    # "high" | "medium" | "low"
    react_reasoning: str | None     # LLM's stated reasoning from submit_fix

    # ── branch / apply ────────────────────────────────────────────────────────
    fix_branch_name: str | None
    apply_error: str | None         # non-None when apply_patch itself crashed

    # ── test ──────────────────────────────────────────────────────────────────
    test_passed: bool | None
    test_output: str | None         # full pytest stdout+stderr

    # ── ci / mr ───────────────────────────────────────────────────────────────
    ci_status: str | None           # "success" | "failed" | "timeout"

    # ── generic error slot ────────────────────────────────────────────────────
    error: str | None
