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
import os
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
from services.llm_model_check import check_or_abort as _check_llm_model
from settings import worker_cfg as cfg

from evaluation.fixture import Fixture, discover

logger = logging.getLogger(__name__)

_RUNS_ROOT = _HERE / "runs"


# ── Agent factory ────────────────────────────────────────────────────────────

def make_agent(agent_spec: dict, *, llm_model_served: str | None = None) -> Agent:
    """Build an Agent instance from a config dict.

    agent_spec = {
        "name": "...",
        "agent": "langgraph",
        "kwargs": {...},
        "enhancements": [{"kind": "memory", ...}, ...]   # optional
    }

    `llm_model_served`, when provided, is stashed on agent_spec so the
    eval-mode RunRecord captures the backend's actually-served model name
    (from llm_model_check.check_or_abort). None for cloud backends.

    Add new agents here as adapters are written (Aider, SWE-agent, ...).
    """
    if llm_model_served is not None:
        # Copy first — never mutate the caller's spec dict.
        agent_spec = {**agent_spec, "llm_model_served": llm_model_served}
    kind = agent_spec.get("agent", "langgraph")
    kwargs = dict(agent_spec.get("kwargs", {}))
    if kind == "langgraph":
        enh_specs = agent_spec.get("enhancements", [])
        if enh_specs:
            kwargs.setdefault("enhancements", build_enhancements(enh_specs))
        # Eval runs do NOT write to the running-mode journal — eval has its own
        # output directory (evaluation/runs/), so journal stays None.
        #
        # checkpointer=None is REQUIRED here, not optional. The checkpointer is
        # keyed on thread_id=bug_id, and in evaluation bug_id is the *fixture
        # id* — identical across every sweep, every spec in a multi-spec
        # config, and every parallel process, all sharing one sqlite file.
        # With checkpointing on, cell N silently *resumes* cell N-1's saved
        # state (the LangGraph resume semantics meant for crash recovery in
        # running mode), cross-contaminating outcomes (observed: a baseline
        # cell inheriting a reflection cell's reflection_count, test_passed
        # leaking, fix_rate flapping run-to-run). Evaluation cells are
        # independent fresh trials — resume is meaningless here. Unless the
        # spec explicitly overrides it, force checkpointing off.
        kwargs.setdefault("checkpointer", None)
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

    # Twin of the checkpointer=None rule in make_agent, for the same reason.
    # The intra-loop step checkpoint (BF_STEP_CHECKPOINT, plan item W2) is keyed
    # on bug_id, which in evaluation is the *fixture id* — identical across
    # specs, sweeps and parallel processes. An inherited BF_STEP_CHECKPOINT from
    # the ambient environment would let one cell resume another's loop state.
    # Eval cells are independent fresh trials; resume is meaningless here.
    os.environ["BF_STEP_CHECKPOINT"] = "none"

    summary: list[dict] = []

    # One-shot startup check: when pointed at a self-hosted backend, verify
    # the served model matches the env. Aborts the whole sweep on mismatch
    # rather than letting cells silently run against the wrong model (eval
    # comparisons would be invalid). Cloud backends are not probed (return
    # None). Override with LLM_ALLOW_MODEL_MISMATCH=1 for known divergence.
    served = _check_llm_model(cfg)

    for spec in agent_specs:
        spec_name = spec.get("name", spec.get("agent", "agent"))
        agent = make_agent(spec, llm_model_served=served)
        for fixture in fixtures:
            cell_dir = run_dir / spec_name / fixture.fixture_id
            cell_dir.mkdir(parents=True, exist_ok=True)
            logger.info("eval cell: agent=%s fixture=%s", spec_name, fixture.fixture_id)

            provider = make_provider(fixture, output_dir=cell_dir / "patch_output")
            # Composite thread_id per cell. bug_id stays = fixture.fixture_id
            # so journal/RunRecord/log lines callers grep on don't shift, but
            # the LangGraph checkpoint key gets the spec_name suffix so a
            # future sweep with checkpointing turned back ON can't cross-
            # contaminate cells that share a fixture_id but run under
            # different agent specs. spec_name is already unique within the
            # sweep (it's the per-cell output-dir component above).
            # Defense-in-depth today: `make_agent` still forces
            # checkpointer=None for every eval cell, so this seam is unused
            # until someone re-enables checkpointing — at which point this
            # split is what keeps the comparison valid. See
            # [[project_eval_checkpoint_contamination]].
            thread_id = f"{fixture.fixture_id}::{spec_name}"
            bug_input = BugInput(
                bug_id=fixture.fixture_id,
                provider=provider,
                thread_id=thread_id,
            )

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
                llm_model=spec.get("llm_model") or getattr(cfg, "llm_model", None),
                llm_model_served=served,
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
