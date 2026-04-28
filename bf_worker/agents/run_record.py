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

    # Telemetry pulled from the LangGraph state when present
    react_step_count:  int | None     = None
    react_confidence:  str | None     = None
    fix_branch_name:   str | None     = None
    test_passed:       bool | None    = None
    suspect_file_path: str | None     = None

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
            timestamp         = ts,
            react_step_count  = s.get("react_step_count"),
            react_confidence  = s.get("react_confidence"),
            fix_branch_name   = s.get("fix_branch_name"),
            test_passed       = s.get("test_passed"),
            suspect_file_path = s.get("suspect_file_path"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    @classmethod
    def from_dict(cls, d: dict) -> "RunRecord":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})  # type: ignore[arg-type]
