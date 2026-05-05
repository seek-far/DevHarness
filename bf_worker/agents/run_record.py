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
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "1"


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

    # Telemetry pulled from the LangGraph state when present
    react_step_count:     int | None     = None
    react_confidence:     str | None     = None
    fix_branch_name:      str | None     = None
    test_passed:          bool | None    = None
    suspect_file_path:    str | None     = None
    parse_trace_fallback: bool | None    = None  # True when parser fell back to raw-trace mode (LLM saw trace, no suspect file)
    source_fetch_failed:  bool | None    = None  # True when parser produced a path but the file couldn't be read (LLM works from trace + parser's hint)
    fetch_trace_retries:  int | None     = None  # number of transient-retry loops the fetch_trace node took (0 = first-attempt success; None = node didn't run / pre-existing record)

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
            timestamp         = ts,
            react_step_count     = s.get("react_step_count"),
            react_confidence     = s.get("react_confidence"),
            fix_branch_name      = s.get("fix_branch_name"),
            test_passed          = s.get("test_passed"),
            suspect_file_path    = s.get("suspect_file_path"),
            parse_trace_fallback = s.get("parse_trace_fallback"),
            source_fetch_failed  = s.get("source_fetch_failed"),
            fetch_trace_retries  = s.get("fetch_trace_retries"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    @classmethod
    def from_dict(cls, d: dict) -> "RunRecord":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]
