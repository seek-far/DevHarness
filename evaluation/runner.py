"""
Runner — sweeps one or more agents across one or more fixtures.

Output: evaluation/runs/<run_id>/<agent_name>/<fixture_id>/
    record.json      — RunRecord (canonical schema; see agents.run_record)
    final_state.json — sanitized graph state (when available)

Plus aggregate evaluation/runs/<run_id>/summary.json with the same RunRecord
shape for every cell, ready for metrics aggregation.
"""

from __future__ import annotations
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure bf_worker/ is importable when running as a module
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT / "bf_worker"))
sys.path.insert(0, str(_ROOT))

from agents.base import Agent, BugInput
from agents.langgraph_agent import LangGraphAgent
from agents.run_record import RunRecord
from providers.local_provider import LocalNoGitProvider
from enhancements import build_enhancements

from evaluation.fixture import Fixture, discover

logger = logging.getLogger(__name__)

_RUNS_ROOT = _HERE / "runs"


# ── Agent factory ────────────────────────────────────────────────────────────

def make_agent(agent_spec: dict) -> Agent:
    """Build an Agent instance from a config dict.

    agent_spec = {
        "name": "...",
        "agent": "langgraph",
        "kwargs": {...},
        "enhancements": [{"kind": "memory", ...}, ...]   # optional
    }

    Add new agents here as adapters are written (Aider, SWE-agent, ...).
    """
    kind = agent_spec.get("agent", "langgraph")
    kwargs = dict(agent_spec.get("kwargs", {}))
    if kind == "langgraph":
        enh_specs = agent_spec.get("enhancements", [])
        if enh_specs:
            kwargs.setdefault("enhancements", build_enhancements(enh_specs))
        # Eval runs do NOT write to the running-mode journal — eval has its own
        # output directory (evaluation/runs/), so journal stays None.
        return LangGraphAgent(agent_config=agent_spec, **kwargs)
    raise ValueError(f"unknown agent kind: {kind!r}")


# ── Provider factory for fixtures ────────────────────────────────────────────

def make_provider(fixture: Fixture, output_dir: Path):
    """Build a provider that exposes the fixture as a bug source.

    Currently piggybacks on LocalNoGitProvider — a fixture is structurally a
    plain directory with an optional trace file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    return LocalNoGitProvider(
        source_dir=str(fixture.source_dir),
        output_dir=str(output_dir),
        trace_file=str(fixture.trace_file) if fixture.trace_file else "",
        test_cmd=fixture.test_cmd,
        bug_id=fixture.fixture_id,
    )


# ── Runner ───────────────────────────────────────────────────────────────────

def run_sweep(
    agent_specs: list[dict],
    fixtures: list[Fixture],
    run_id: str | None = None,
) -> Path:
    """Run every (agent_spec × fixture) combination, write results, return run dir."""
    run_id = run_id or datetime.now(timezone.utc).strftime("run_%Y%m%dT%H%M%SZ")
    run_dir = _RUNS_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    summary: list[dict] = []

    for spec in agent_specs:
        spec_name = spec.get("name", spec.get("agent", "agent"))
        agent = make_agent(spec)
        for fixture in fixtures:
            cell_dir = run_dir / spec_name / fixture.fixture_id
            cell_dir.mkdir(parents=True, exist_ok=True)
            logger.info("eval cell: agent=%s fixture=%s", spec_name, fixture.fixture_id)

            provider = make_provider(fixture, output_dir=cell_dir / "patch_output")
            bug_input = BugInput(bug_id=fixture.fixture_id, provider=provider)

            t0 = time.monotonic()
            err = None
            fix_output = None
            try:
                fix_output = agent.fix(bug_input)
            except Exception as exc:
                logger.exception("eval cell crashed")
                err = str(exc)
            elapsed = time.monotonic() - t0

            record = RunRecord.from_outputs(
                agent_name=spec_name,
                bug_id=fixture.fixture_id,
                outcome=fix_output.outcome if fix_output else "error",
                error=fix_output.error if fix_output else err,
                iterations=fix_output.iterations if fix_output else 0,
                final_state=fix_output.final_state if fix_output else None,
                elapsed_s=round(elapsed, 3),
                agent_config=spec,
                run_id=run_id,
            )
            cell_record = record.to_dict()
            cell_record.update({
                "fixture_id":       fixture.fixture_id,
                "category":         fixture.category,
                "difficulty":       fixture.difficulty,
                "expected_outcome": fixture.expected_outcome,
                "matches_expected": record.outcome == fixture.expected_outcome,
            })
            (cell_dir / "record.json").write_text(
                json.dumps(cell_record, indent=2, default=str), encoding="utf-8"
            )

            if fix_output and fix_output.final_state is not None:
                (cell_dir / "final_state.json").write_text(
                    json.dumps(fix_output.final_state, indent=2, default=str),
                    encoding="utf-8",
                )

            summary.append(cell_record)

    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    logger.info("sweep complete: %s (%d cells)", run_dir, len(summary))
    return run_dir
