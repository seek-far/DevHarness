"""
RunRecord — the canonical structured outcome of one agent.fix() invocation.

Used in two places:
  - bf_worker/journal.py  (running mode auto-capture)
  - evaluation/runner.py  (evaluation-mode sweep cells)

Both read and write the same shape so downstream tooling (metrics, promotion,
external dashboards) only needs to handle one schema. Bump SCHEMA_VERSION when
making incompatible changes.
"""

from __future__ import annotations
import json
import subprocess
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_GIT_STATUS_MAX_CHARS = 4000


def _git_output(*args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _worker_cfg():
    """Lazily fetch worker settings. Lazy because run_record is imported by the
    evaluation tooling, which must not require a fully-built settings tree."""
    try:
        from settings import worker_cfg

        return worker_cfg
    except Exception:  # pragma: no cover - defensive
        return None


def _agent_code_git_info() -> dict[str, Any]:
    """Best-effort git version snapshot for this SDLCMA checkout."""
    status = _git_output("status", "--short")
    if status is not None and len(status) > _GIT_STATUS_MAX_CHARS:
        status = status[:_GIT_STATUS_MAX_CHARS] + "\n...[truncated]"

    return {
        "agent_code_git_commit": _git_output("rev-parse", "HEAD"),
        "agent_code_git_branch": _git_output("branch", "--show-current")
        or _git_output("rev-parse", "--abbrev-ref", "HEAD"),
        "agent_code_git_dirty": bool(status) if status is not None else None,
        "agent_code_git_status": status,
    }


@dataclass
class RunRecord:
    schema_version:    str
    agent_name:        str
    bug_id:            str
    outcome:           str            # "fixed" | "no_fix" | "error" | "already_fixed"
                                      # "already_fixed" is the R10 short-circuit:
                                      # the deterministic fix branch already had a merged MR,
                                      # so the run skipped apply/commit/MR.
    timestamp:         str            # ISO8601 UTC, e.g. 20260427T161524Z
    error:             str | None     = None
    iterations:        int            = 0
    elapsed_s:         float | None   = None

    # Identity / context
    project_id:        str            = ""
    project_web_url:   str            = ""
    job_id:            str            = ""
    agent_config:      dict           = field(default_factory=dict)
    run_id:            str | None     = None  # set by evaluation runner; None in journal mode
    llm_model:         str | None     = None  # the LLM model used for this run (e.g. cfg.llm_model)
    agent_code_git_commit: str | None = None  # git commit of this SDLCMA/agent checkout
    agent_code_git_branch: str | None = None  # git branch of this SDLCMA/agent checkout
    agent_code_git_dirty:  bool | None = None # True when this SDLCMA/agent checkout had local changes
    agent_code_git_status: str | None = None  # compact `git status --short` for this SDLCMA/agent checkout

    # Telemetry pulled from the LangGraph state when present
    react_step_count:     int | None     = None
    react_confidence:     str | None     = None
    fix_branch_name:      str | None     = None
    branch_create_status: str | None     = None  # "success" (created) | "reused" (already existed)
    base_branch:          str | None     = None
    base_commit:          str | None     = None
    commit_status:        str | None     = None  # "success" | "reused" (same tree on remote, no push) | "updated" (force-pushed over stale fix) | "no_changes"
    commit_branch:        str | None     = None
    commit_hash:          str | None     = None
    review_status:        str | None     = None  # "opened" | "reused" (existing open MR) | "already_merged" (R10) | "local_commit" (LocalGit) | "report_generated" (no-git)
    review_url:           str | None     = None
    review_id:            int | str | None = None
    review_iid:           int | str | None = None
    review_branch:        str | None     = None
    patch_file:           str | None     = None
    report_file:          str | None     = None
    branch_create_result: dict | None    = None
    commit_result:        dict | None    = None
    review_result:        dict | None    = None
    test_passed:          bool | None    = None
    suspect_file_path:    str | None     = None
    parse_trace_fallback: bool | None    = None  # True when parser fell back to raw-trace mode (LLM saw trace, no suspect file)
    source_fetch_failed:  bool | None    = None  # True when parser produced a path but the file couldn't be read (LLM works from trace + parser's hint)
    fetch_trace_retries:       int | None = None  # number of transient-retry loops the fetch_trace node took (0 = first-attempt success; None = node didn't run / pre-existing record)
    fetch_source_file_retries: int | None = None  # transient retries inside fetch_source_file before the read succeeded (None when the run never reached the node or fell through to source_fetch_failed)
    commit_change_retries:     int | None = None  # transient retries inside commit_change before commit_and_push succeeded
    wait_ci_result_retries:    int | None = None  # transient retries inside wait_ci_result before the provider returned (0 even on timeout: timeouts are not transients)
    create_mr_retries:         int | None = None  # transient retries inside create_mr before create_review succeeded
    reflection_count:          int | None = None  # post-mortems the reflection enhancement produced this run (None when the enhancement is not wired; ≤ MAX_FIX_RETRIES otherwise)
    reflection_mode:           str | None = None  # last reflection lens: "apply" (deterministic patch-mechanics note, no LLM) | "test" (LLM causal post-mortem) | None (enhancement not wired / never fired)
    code_review_status:         str | None = None  # Phase-2 reviewer: None=disabled | clean | advisory | would_escalate (shadow) | escalated/rounds_exhausted (acting) | skipped_budget | skipped_no_files | error
    code_review_finding_count:  int | None = None  # findings the inspector returned
    code_review_would_escalate: bool | None = None  # True iff a high-sev AND high-conf finding was present (shadow records it; acting acts on it)
    code_review_findings:       list | None = None  # compact per-finding dicts for offline evaluation of the reviewer's value
    code_review_rounds:         int | None = None  # acting mode only: fixer↔review rounds escalated (independent of fix_retry_count; None/0 in shadow)
    max_input_tokens:           int | None = None  # largest prompt_tokens reported by the backend across every LLM call this run (react_loop + reflection); 0 when backend never returned usage; None when no LLM call ever ran
    # ── per-run LLM latency / cost telemetry (additive; SCHEMA_VERSION="1") ──
    # All accumulate across react_loop + reflection LLM calls within one fix().
    # None across the board when the run never made an LLM call (e.g. R10
    # short-circuit). total_cached_input_tokens stays None on backends that
    # don't report prompt caching (most self-hosted today) — distinct from 0
    # which means "reported but nothing cached".
    llm_call_count:             int   | None = None
    total_prompt_tokens:        int   | None = None
    total_completion_tokens:    int   | None = None
    total_cached_input_tokens:  int   | None = None
    # ── cost (additive 2026-07-14; SCHEMA_VERSION unchanged at "1") ─────
    # USD for this run's LLM calls. In gateway mode the gateway is
    # authoritative (it alone knows which backend the policy picked and what
    # that backend charges, and a fallback ladder can touch two prices in one
    # run); in direct mode the worker prices it from configs/pricing.yaml by
    # LLM_MODEL. None = unpriced backend or unknown model — deliberately NOT
    # 0.0, which would claim the run was free.
    total_cost_usd:             float | None = None
    cost_currency:              str   | None = None
    cost_source:                str   | None = None  # "gateway" | "local_pricing" | None
    total_llm_wallclock_s:      float | None = None
    # Per-call wallclock breakdown (ms, ordered). Surface for p50/p95/p99
    # per-call latency and for spotting which calls dominated the run
    # without parsing logs. Accumulates across react_loop + reflection
    # the same way total_llm_wallclock_s does, and stays None when no LLM
    # call ever ran (R10 short-circuit). SCHEMA_VERSION unchanged
    # (additive, backward-compatible).
    llm_call_wallclock_ms:      list  | None = None
    # vLLM /v1/models actually-served name (self-hosted only; None for cloud
    # backends). Distinct from llm_model (which is what the env file declared)
    # so a silent mismatch is recoverable post-hoc; mismatches at startup
    # already abort unless LLM_ALLOW_MODEL_MISMATCH=1.
    llm_model_served:           str   | None = None
    # ── llm_gateway routing (additive 2026-05-25; SCHEMA_VERSION="1") ────────
    # Name of the backend the SDLCMA llm_gateway actually routed this run's
    # last LLM call to. None when the worker is talking to a backend directly
    # (no gateway in the path) or when the gateway didn't return the header.
    # Distinct from llm_model / llm_model_served: those describe the model
    # identifier; llm_backend_name identifies the *backend* (e.g.
    # "qwen3_dashscope" vs "local_vllm") so per-backend aggregations are
    # possible even when several backends serve the same model name.
    llm_backend_name:           str   | None = None
    # ── no_fix retry telemetry (additive 2026-05-25; SCHEMA_VERSION="1") ─────
    # Counts how many times react_loop exited with llm_result=None and the
    # graph re-entered react_loop to give a different backend a shot. Gated
    # on cfg.llm_via_gateway in the router; capped at NO_FIX_MAX_RETRIES=1.
    # 0 = react_loop succeeded first try (or exhausted but gateway off);
    # 1 = first react_loop exhausted, second entry happened;
    # 2 = both react_loop entries exhausted, run went to handle_failure.
    # None when no react_loop run ever happened (e.g. R10 short-circuit).
    no_fix_retry_count:         int   | None = None
    # ── SWE-bench (additive 2026-07-18; SCHEMA_VERSION unchanged at "1") ──────
    # Set only by the mini-swe-agent substrate path (bf_worker/swebench_single.py
    # + MiniSweAgent). None for every GitLab/standalone/local run — same
    # additive-field convention as the blocks above. `resolved` is the official
    # SWE-bench harness verdict (FAIL_TO_PASS/PASS_TO_PASS), computed AFTER
    # fix() by the entry point, not by the agent. None = harness not run /
    # not a SWE-bench run — deliberately NOT False, which would claim the
    # patch was graded and failed.
    swebench_instance_id:       str   | None = None
    resolved:                   bool  | None = None

    # ── intra-loop step checkpoint (plan item W2; BF_STEP_CHECKPOINT) ─────────
    # All None unless the run had step checkpointing on — "never resumed" and
    # "resume was not enabled" are different facts, and a confident 0 would
    # conflate them. Additive, so SCHEMA_VERSION is unchanged.
    #   step_resume_count           how many times the loop restored a prefix
    #   step_resumed_from_step      loop iteration it continued from
    #   step_replayed_command_count commands re-issued because the in-container
    #                               marker said they MIGHT already have run —
    #                               the measurement of at-least-once execution
    #                               that was previously invisible
    step_resume_count:           int  | None = None
    step_resumed_from_step:      int  | None = None
    step_replayed_command_count: int  | None = None

    # ── construction ─────────────────────────────────────────────────────────

    @classmethod
    def from_outputs(
        cls,
        *,
        agent_name: str,
        bug_id: str,
        project_id: str = "",
        project_web_url: str = "",
        job_id: str = "",
        outcome: str,
        error: str | None,
        iterations: int,
        final_state: dict | None,
        elapsed_s: float | None = None,
        agent_config: dict | None = None,
        run_id: str | None = None,
        llm_model: str | None = None,
        llm_model_served: str | None = None,
    ) -> "RunRecord":
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        s = final_state or {}
        agent_git = _agent_code_git_info()
        return cls(
            schema_version    = SCHEMA_VERSION,
            agent_name        = agent_name,
            bug_id            = bug_id,
            project_id        = project_id,
            project_web_url   = project_web_url,
            job_id            = job_id,
            outcome           = outcome,
            error             = error,
            iterations        = iterations,
            elapsed_s         = elapsed_s,
            agent_config      = agent_config or {},
            run_id            = run_id,
            llm_model         = llm_model,
            agent_code_git_commit = agent_git["agent_code_git_commit"],
            agent_code_git_branch = agent_git["agent_code_git_branch"],
            agent_code_git_dirty  = agent_git["agent_code_git_dirty"],
            agent_code_git_status = agent_git["agent_code_git_status"],
            timestamp         = ts,
            react_step_count     = s.get("react_step_count"),
            react_confidence     = s.get("react_confidence"),
            fix_branch_name      = s.get("fix_branch_name"),
            branch_create_status = s.get("branch_create_status"),
            base_branch          = s.get("base_branch"),
            base_commit          = s.get("base_commit"),
            commit_status        = s.get("commit_status"),
            commit_branch        = s.get("commit_branch"),
            commit_hash          = s.get("commit_hash"),
            review_status        = s.get("review_status"),
            review_url           = s.get("review_url"),
            review_id            = s.get("review_id"),
            review_iid           = s.get("review_iid"),
            review_branch        = s.get("review_branch"),
            patch_file           = s.get("patch_file"),
            report_file          = s.get("report_file"),
            branch_create_result = s.get("branch_create_result"),
            commit_result        = s.get("commit_result"),
            review_result        = s.get("review_result"),
            test_passed          = s.get("test_passed"),
            suspect_file_path    = s.get("suspect_file_path"),
            parse_trace_fallback = s.get("parse_trace_fallback"),
            source_fetch_failed  = s.get("source_fetch_failed"),
            fetch_trace_retries       = s.get("fetch_trace_retries"),
            fetch_source_file_retries = s.get("fetch_source_file_retries"),
            commit_change_retries     = s.get("commit_change_retries"),
            wait_ci_result_retries    = s.get("wait_ci_result_retries"),
            create_mr_retries         = s.get("create_mr_retries"),
            reflection_count          = s.get("reflection_count"),
            reflection_mode           = s.get("reflection_mode"),
            code_review_status         = s.get("code_review_status"),
            code_review_finding_count  = s.get("code_review_finding_count"),
            code_review_would_escalate = s.get("code_review_would_escalate"),
            code_review_findings       = s.get("code_review_findings"),
            code_review_rounds         = s.get("code_review_rounds"),
            max_input_tokens           = s.get("max_input_tokens"),
            llm_call_count             = s.get("llm_call_count"),
            total_prompt_tokens        = s.get("total_prompt_tokens"),
            total_completion_tokens    = s.get("total_completion_tokens"),
            total_cached_input_tokens  = s.get("total_cached_input_tokens"),
            total_cost_usd             = s.get("total_cost_usd"),
            cost_currency              = ("USD" if s.get("total_cost_usd") is not None else None),
            # An agent that knows its own cost source (e.g. the mini-swe-agent
            # substrate → "mini_litellm") sets it in state; otherwise fall back
            # to the gateway-vs-direct inference the LangGraph path relies on.
            cost_source                = (
                s.get("cost_source")
                or (
                    ("gateway" if getattr(_worker_cfg(), "llm_via_gateway", False) else "local_pricing")
                    if s.get("total_cost_usd") is not None else None
                )
            ),
            total_llm_wallclock_s      = s.get("total_llm_wallclock_s"),
            llm_call_wallclock_ms      = s.get("llm_call_wallclock_ms"),
            llm_model_served           = llm_model_served,
            llm_backend_name           = s.get("llm_backend_name"),
            no_fix_retry_count         = s.get("no_fix_retry_count"),
            swebench_instance_id       = s.get("swebench_instance_id"),
            resolved                   = s.get("resolved"),
            step_resume_count           = s.get("step_resume_count"),
            step_resumed_from_step      = s.get("step_resumed_from_step"),
            step_replayed_command_count = s.get("step_replayed_command_count"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    @classmethod
    def from_dict(cls, d: dict) -> "RunRecord":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]
