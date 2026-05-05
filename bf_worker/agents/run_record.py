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
    outcome:           str            # "fixed" | "no_fix" | "error"
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
    branch_create_status: str | None     = None
    base_branch:          str | None     = None
    base_commit:          str | None     = None
    commit_status:        str | None     = None
    commit_branch:        str | None     = None
    commit_hash:          str | None     = None
    review_status:        str | None     = None
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
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    @classmethod
    def from_dict(cls, d: dict) -> "RunRecord":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]
