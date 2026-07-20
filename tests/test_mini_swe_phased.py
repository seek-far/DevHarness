"""Two-phase waterfall workflow (workflow_mode=1) — no docker/LLM/network.

Drives MiniSweAgent's phased path with one shared DeterministicModel (its output
index continues across the two phases) + one shared LocalEnvironment (the same
"container" both phases run in). Pins:

  - Phase 1 submits a handoff via the sentinel → Phase 2 is seeded with it and
    submits the patch → outcome "fixed", patch captured, handoff recorded;
  - the shared environment is reused across phases (world state persists);
  - a limit-exhausted Phase 1 (empty handoff) still runs Phase 2;
  - workflow_mode is recorded and the agent name encodes the mode.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "bf_worker"))

from agents.base import BugInput  # noqa: E402
from agents.mini_swe_agent import MiniSweAgent  # noqa: E402

from minisweagent.environments.local import LocalEnvironment  # noqa: E402
from minisweagent.models.test_models import DeterministicModel, make_output  # noqa: E402

_INSTANCE = {"instance_id": "sympy__sympy-1", "problem_statement": "x is off by one"}

_HANDOFF = "ROOT_CAUSE: off by one\nSUSPECT_FILES: x.py\nREPRO: python repro.py\n"
_PATCH = "diff --git a/x.py b/x.py\n+    return n - 1\n"

_SUBMIT_HANDOFF = (
    "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\n"
    "ROOT_CAUSE: off by one\\nSUSPECT_FILES: x.py\\nREPRO: python repro.py\\n'"
)
_SUBMIT_PATCH = (
    "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\n"
    "diff --git a/x.py b/x.py\\n+    return n - 1\\n'"
)


def _bug():
    return BugInput(bug_id="SWE-1", provider=None, metadata={"swebench_instance": _INSTANCE})


def test_two_phase_waterfall_fixed(tmp_path):
    # Shared model: outputs consumed sequentially across both phases.
    model = DeterministicModel(outputs=[
        make_output("investigating", [{"command": "echo investigating"}]),   # phase 1 step
        make_output("hand off", [{"command": _SUBMIT_HANDOFF}]),             # phase 1 submit
        make_output("fixing", [{"command": "echo fixing"}]),                 # phase 2 step
        make_output("submit patch", [{"command": _SUBMIT_PATCH}]),           # phase 2 submit
    ])
    env = LocalEnvironment(cwd=str(tmp_path))
    agent = MiniSweAgent(model=model, env=env, mini_config={"agent": {}}, workflow_mode=1)

    out = agent.fix(_bug())

    assert out.outcome == "fixed"
    assert agent.name == "mini_swe_agent_wf1"
    state = out.final_state or {}
    assert state["model_patch"] == _PATCH
    assert state["workflow_mode"] == 1
    assert state["phase1_handoff"] == _HANDOFF          # phase-1 submission carried over
    assert state["phase1_calls"] == 2 and state["phase2_calls"] == 2
    assert state["llm_call_count"] == 4                 # summed across phases


def test_shared_env_reused_across_phases(tmp_path):
    # A file the phase-1 step writes must be visible to the phase-2 step, since
    # both phases share the same environment ("container persists").
    marker = tmp_path / "from_phase1.txt"
    model = DeterministicModel(outputs=[
        make_output("write marker", [{"command": f"echo hi > {marker}"}]),   # phase 1 writes
        make_output("hand off", [{"command": _SUBMIT_HANDOFF}]),
        make_output("read marker", [{"command": f"cat {marker}"}]),          # phase 2 reads
        make_output("submit", [{"command": _SUBMIT_PATCH}]),
    ])
    env = LocalEnvironment(cwd=str(tmp_path))
    agent = MiniSweAgent(model=model, env=env, mini_config={"agent": {}}, workflow_mode=1)

    out = agent.fix(_bug())

    assert out.outcome == "fixed"
    assert marker.exists()  # written in phase 1, still there for phase 2


def test_phase1_limit_exhausted_still_runs_phase2(tmp_path):
    # Phase 1 hits its step limit without submitting (empty handoff); phase 2
    # still runs and can submit.
    model = DeterministicModel(outputs=[
        make_output("looking", [{"command": "echo looking"}]),  # phase 1 (no submit)
        make_output("fixing", [{"command": "echo fixing"}]),    # phase 2 step
        make_output("submit", [{"command": _SUBMIT_PATCH}]),    # phase 2 submit
    ])
    env = LocalEnvironment(cwd=str(tmp_path))
    agent = MiniSweAgent(model=model, env=env, mini_config={"agent": {}}, workflow_mode=1)
    # Shrink phase-1's step budget to 1 so it exits (LimitsExceeded, no submit)
    # after a single non-submitting step. _run_phased imports the limit at call
    # time, so patching the module attribute takes effect.
    import agents.mini_phases as ph
    orig = ph.INVESTIGATE_STEP_LIMIT
    ph.INVESTIGATE_STEP_LIMIT = 1
    try:
        out = agent.fix(_bug())
    finally:
        ph.INVESTIGATE_STEP_LIMIT = orig

    assert out.outcome == "fixed"
    state = out.final_state or {}
    assert state["phase1_handoff"] == ""   # phase 1 produced no handoff
    assert state["model_patch"] == _PATCH  # phase 2 still fixed it


def test_phased_writes_two_trajectories_and_sums_tokens(tmp_path):
    def ou(content, actions, pt, ct):
        o = make_output(content, actions)
        o["extra"]["response"] = {"usage": {"prompt_tokens": pt, "completion_tokens": ct}}
        return o

    model = DeterministicModel(outputs=[
        ou("investigate", [{"command": "echo x"}], 500, 20),       # phase 1
        ou("handoff", [{"command": _SUBMIT_HANDOFF}], 600, 30),    # phase 1 submit
        ou("fix", [{"command": "echo y"}], 400, 25),               # phase 2
        ou("submit", [{"command": _SUBMIT_PATCH}], 700, 40),       # phase 2 submit
    ])
    env = LocalEnvironment(cwd=str(tmp_path))
    agent = MiniSweAgent(model=model, env=env, mini_config={"agent": {}},
                         workflow_mode=1, trajectory_dir=tmp_path)

    state = agent.fix(_bug()).final_state or {}

    # tokens summed across BOTH phases; max_input is the peak single call.
    assert state["total_prompt_tokens"] == 500 + 600 + 400 + 700
    assert state["total_completion_tokens"] == 20 + 30 + 25 + 40
    assert state["max_input_tokens"] == 700
    # one trajectory file per phase (so per-phase context can be inspected)
    assert (tmp_path / "sympy__sympy-1.phase1.json").exists()
    assert (tmp_path / "sympy__sympy-1.phase2.json").exists()


# ── mode 3: two-phase with bounded back-edge ─────────────────────────────────

_V3_HANDOFF = (
    "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\nROOT_CAUSE: off by one\\n"
    "SUSPECT_FILES: x.py\\nREPRO: python repro.py\\nDETAILED_SUMMARY: lots of detail\\n'"
)
_V3_REINVEST = (
    "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\nREQUEST_REINVESTIGATION\\n"
    "WHY_STUCK: root cause is elsewhere\\nDETAILED_SUMMARY: tried A, it failed\\n'"
)
_V3_PATCH = _SUBMIT_PATCH  # a normal git-diff submission


def test_mode3_back_edge_loop_then_fix(tmp_path):
    model = DeterministicModel(outputs=[
        make_output("investigate r0", [{"command": "echo inv0"}]),
        make_output("hand off r0", [{"command": _V3_HANDOFF}]),
        make_output("try fix r0", [{"command": "echo solve0"}]),
        make_output("request reinvestigation", [{"command": _V3_REINVEST}]),   # ← back-edge
        make_output("re-investigate r1", [{"command": "echo inv1"}]),
        make_output("hand off r1", [{"command": _V3_HANDOFF}]),
        make_output("fix r1", [{"command": "echo solve1"}]),
        make_output("submit patch", [{"command": _V3_PATCH}]),
    ])
    env = LocalEnvironment(cwd=str(tmp_path))
    agent = MiniSweAgent(model=model, env=env, mini_config={"agent": {}},
                         workflow_mode=3, trajectory_dir=tmp_path)

    out = agent.fix(_bug())

    assert out.outcome == "fixed"
    assert agent.name == "mini_swe_agent_wf3"
    state = out.final_state or {}
    assert state["model_patch"] == _PATCH
    assert state["v3_back_edges"] == 1                       # one re-investigation round
    # a trajectory per phase per round (2 rounds → 4 files)
    for name in ("investigate0", "solve0", "investigate1", "solve1"):
        assert (tmp_path / f"sympy__sympy-1.{name}.json").exists()


def test_mode3_no_backedge_when_solve_patches_first(tmp_path):
    model = DeterministicModel(outputs=[
        make_output("investigate", [{"command": "echo inv"}]),
        make_output("hand off", [{"command": _V3_HANDOFF}]),
        make_output("fix", [{"command": "echo solve"}]),
        make_output("submit patch", [{"command": _V3_PATCH}]),
    ])
    env = LocalEnvironment(cwd=str(tmp_path))
    agent = MiniSweAgent(model=model, env=env, mini_config={"agent": {}}, workflow_mode=3)

    out = agent.fix(_bug())

    assert out.outcome == "fixed"
    assert (out.final_state or {})["v3_back_edges"] == 0     # solve patched on round 0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
