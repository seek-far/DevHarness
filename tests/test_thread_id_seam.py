"""Tests for the thread_id seam (Item 2).

Background: the LangGraph checkpointer is keyed on ``thread_id``. In
running mode (GitLab worker, standalone) ``thread_id == bug_id`` is the
correct unit-of-work — same bug across worker restarts shares a thread,
which is exactly how resume is supposed to work. In evaluation mode
``bug_id`` is the *fixture id*, identical across every cell, spec, and
parallel process — so once anyone re-enables checkpointing in eval, the
shared key cross-contaminates results (the same family of bugs that
made us force ``checkpointer=None`` per cell in the runner today; see
``project_eval_checkpoint_contamination``).

This module pins the seam, not the eval-mode contamination itself —
eval still forces ``checkpointer=None`` so the contamination can't fire
today. The seam is defense-in-depth for when that changes.

Coverage:
  * BugInput.thread_id wins over bug_id when set;
  * BugInput without thread_id falls back to bug_id (running modes are
    byte-identical to the pre-Item-2 contract);
  * The evaluation runner stamps a per-cell ``thread_id`` of
    ``f"{fixture_id}::{spec_name}"`` on every BugInput, distinguishing
    cells that share a fixture across specs.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from agents.base import BugInput  # noqa: E402
from agents.langgraph_agent import LangGraphAgent  # noqa: E402


# ── LangGraphAgent: thread_id flows into runtime config ─────────────────────


class _StubProvider:
    """No-op provider — the graph never runs in these tests."""

    def fetch_trace(self, *_a, **_k):
        return ""

    def ensure_repo_ready(self, *_a, **_k):
        return None


def _make_agent() -> LangGraphAgent:
    # checkpointer=None keeps the agent from touching any sqlite path during
    # unit tests; we only care which thread_id was passed to graph.invoke.
    return LangGraphAgent(checkpointer=None)


def _captured_thread_id(agent: LangGraphAgent, bug_input: BugInput) -> str:
    """Run agent.fix() with graph.invoke patched to capture the runtime
    config's thread_id, then bail out (we don't need the real graph to
    execute for this seam test)."""
    captured: dict = {}

    def _spy_invoke(_state, config=None, **_kwargs):
        captured["thread_id"] = config["configurable"]["thread_id"]
        # Return a minimal final_state so fix() can synthesise a FixOutput
        # without complaint. budget snapshot is taken from a real RunBudget
        # the agent created — we don't need to set it here.
        return {}

    with patch.object(agent._graph, "invoke", side_effect=_spy_invoke):
        agent.fix(bug_input)
    return captured["thread_id"]


def test_thread_id_falls_back_to_bug_id_when_unset():
    """Running modes (GitLab worker, standalone) never set thread_id;
    behaviour MUST be byte-identical to the pre-Item-2 contract."""
    agent = _make_agent()
    bug_input = BugInput(bug_id="BUG-123", provider=_StubProvider())
    assert _captured_thread_id(agent, bug_input) == "BUG-123"


def test_thread_id_wins_over_bug_id_when_set():
    """Callers (eval runner) override the checkpoint key without
    touching bug_id, which downstream tooling/log lines grep on."""
    agent = _make_agent()
    bug_input = BugInput(
        bug_id="F01",
        provider=_StubProvider(),
        thread_id="F01::baseline",
    )
    assert _captured_thread_id(agent, bug_input) == "F01::baseline"


def test_empty_string_thread_id_falls_back_to_bug_id():
    """``thread_id=""`` (the dataclass default) is treated as unset —
    falsy short-circuits to bug_id rather than producing an empty
    checkpoint key (which would silently glue every default run
    together)."""
    agent = _make_agent()
    bug_input = BugInput(bug_id="BUG-Y", provider=_StubProvider(), thread_id="")
    assert _captured_thread_id(agent, bug_input) == "BUG-Y"


# ── evaluation/runner: per-cell composite thread_id ─────────────────────────


def test_eval_runner_stamps_composite_thread_id_per_cell(tmp_path):
    """The runner composes ``f"{fixture_id}::{spec_name}"`` per cell,
    so a future checkpointing-on sweep can't share keys between cells
    that happen to use the same fixture under different agent specs."""
    from evaluation import runner as eval_runner
    from evaluation.fixture import Fixture

    fixture = Fixture(
        fixture_id="F01",
        source_dir=tmp_path,
        trace_file=None,
    )

    captured: list[BugInput] = []

    class _RecordingAgent:
        name = "recording"

        def fix(self, bug_input: BugInput):
            captured.append(bug_input)
            from agents.base import FixOutput
            return FixOutput(outcome="fixed", bug_id=bug_input.bug_id,
                             iterations=0, final_state={})

    # Stub the agent factory so the sweep doesn't try to build a real
    # LangGraphAgent (no LLM env required). Skip the LLM-model probe
    # for the same reason.
    with patch.object(eval_runner, "make_agent", lambda spec, **_kw: _RecordingAgent()), \
         patch.object(eval_runner, "_check_llm_model", lambda *_a, **_k: None), \
         patch.object(eval_runner, "make_provider", lambda *_a, **_k: _StubProvider()):
        eval_runner.run_sweep(
            agent_specs=[
                {"name": "baseline", "agent": "langgraph"},
                {"name": "reflection", "agent": "langgraph"},
            ],
            fixtures=[fixture],
            run_id="test_thread_id_seam",
        )

    # One cell per (spec, fixture); both keep bug_id = fixture_id but
    # the thread_id splits them so a checkpointing-on sweep wouldn't
    # cross-contaminate.
    assert len(captured) == 2
    assert {b.bug_id for b in captured} == {"F01"}
    assert {b.thread_id for b in captured} == {"F01::baseline", "F01::reflection"}
