"""
LangGraphAgent — wraps the LangGraph state machine and exposes hook points.

This is one Agent implementation. Per-LangGraphAgent enhancements (memory
lookup, multi-hypothesis, edge-case test generation, ...) plug in via the
HookRegistry — no changes to the core graph builder, nodes, or providers.

`enhancements` is a list of (hook_name, callback) tuples. They are wired into
a fresh HookRegistry on each fix() call, then attached to the initial state so
graph nodes can invoke them at extension points.
"""

from __future__ import annotations
import logging
import time
from typing import Any, Iterable

from agents.base import Agent, BugInput, FixOutput, Outcome
from agents.run_record import RunRecord
from enhancements.hooks import HookName, HookRegistry
from graph.builder import build_graph
from graph.state import BugFixState
from services.budget import RunBudget
from services.checkpointer import build_checkpointer
from settings import worker_cfg as cfg

logger = logging.getLogger(__name__)


class LangGraphAgent(Agent):
    name = "langgraph"

    def __init__(
        self,
        journal: Any = None,
        enhancements: Iterable[tuple[str, Any]] | None = None,
        agent_config: dict | None = None,
        checkpointer: Any = "auto",
    ):
        """
        checkpointer:
          "auto"  — pick a backend per BF_CHECKPOINT_BACKEND (default: sqlite)
          None    — disable checkpointing (graph compiled without one)
          object  — use the given checkpointer instance (tests inject MemorySaver)
        """
        self._journal = journal
        self._enhancements = list(enhancements or [])
        self._agent_config = agent_config or {}

        if checkpointer == "auto":
            self._checkpointer = build_checkpointer()
        else:
            self._checkpointer = checkpointer

        # Compile the graph once per agent instance. The checkpointer is
        # baked in here; thread_id is supplied per-run via config.
        self._graph = build_graph(checkpointer=self._checkpointer)

    # ── Public API ────────────────────────────────────────────────────────────

    def fix(self, bug_input: BugInput) -> FixOutput:
        hooks = self._build_hooks()
        budget = RunBudget()

        # State carries only serializable, flow-between-nodes data. Provider,
        # hooks, and budget are run-scoped runtime context — they go into
        # `config["configurable"]` instead, which LangGraph passes to nodes
        # but does NOT include in checkpoints. This is what makes resume
        # possible: the checkpoint stores state, the new process supplies
        # config on resume.
        initial_state: BugFixState = {
            "bug_id":          bug_input.bug_id,
            "project_id":      bug_input.project_id,
            "project_web_url": bug_input.project_web_url,
            "job_id":          bug_input.job_id,
            "llm_retry_count": 0,
            "fix_retry_count": 0,
        }
        runtime_config = {
            "configurable": {
                "provider":  bug_input.provider,
                "hooks":     hooks,
                "budget":    budget,
                # thread_id keys the checkpoint store. bug_id is the natural
                # choice — same bug across restarts shares a thread, which is
                # how resume works. Same key as the idempotency layer's dedup
                # key (intentional: "this bug fix" is one logical unit
                # everywhere in the system).
                "thread_id": bug_input.bug_id,
            }
        }

        # Agent-boundary pre-fix hook (e.g. memory lookup injects prior fix patterns)
        initial_state = hooks.run(HookName.AGENT_PRE_FIX, initial_state)

        logger.info("agent=%s bug=%s starting (hooks=%s)",
                    self.name, bug_input.bug_id, hooks)

        t0 = time.monotonic()
        try:
            final_state = self._graph.invoke(initial_state, config=runtime_config)
        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.exception("agent=%s bug=%s crashed", self.name, bug_input.bug_id)
            output = FixOutput(
                outcome="error",
                bug_id=bug_input.bug_id,
                error=str(exc),
            )
            self._maybe_journal(bug_input, output, None, elapsed)
            return output
        elapsed = time.monotonic() - t0

        # Snapshot the budget into final_state so journal/RunRecord can record
        # actual spend without keeping a non-serializable RunBudget in state.
        final_state = dict(final_state)
        final_state["budget"] = budget.to_dict()

        # Agent-boundary post-fix hook (e.g. write outcome to memory store)
        final_state = hooks.run(HookName.AGENT_POST_FIX, final_state)

        if final_state.get("error"):
            outcome: Outcome = "error"
        elif final_state.get("already_fixed"):
            # R10: a merged MR was found for the deterministic fix branch.
            # No commit / MR was made this run — distinguishing this from
            # "fixed" lets evaluation tooling spot the short-circuit.
            outcome = "already_fixed"
        else:
            outcome = "fixed"

        output = FixOutput(
            outcome=outcome,
            bug_id=bug_input.bug_id,
            error=final_state.get("error"),
            iterations=int(final_state.get("fix_retry_count") or 0),
            final_state=_sanitize_state(final_state),
        )

        self._maybe_journal(bug_input, output, final_state, elapsed)
        return output

    # ── Internals ─────────────────────────────────────────────────────────────

    def _build_hooks(self) -> HookRegistry:
        hooks = HookRegistry()
        for hook_name, fn in self._enhancements:
            hooks.register(hook_name, fn)
        return hooks

    def _maybe_journal(
        self,
        bug_input: BugInput,
        output: FixOutput,
        raw_state: dict | None,
        elapsed_s: float,
    ) -> None:
        if self._journal is None:
            return
        try:
            record = RunRecord.from_outputs(
                agent_name=self.name,
                bug_id=bug_input.bug_id,
                project_id=bug_input.project_id,
                project_web_url=bug_input.project_web_url,
                job_id=bug_input.job_id,
                outcome=output.outcome,
                error=output.error,
                iterations=output.iterations,
                final_state=output.final_state,
                elapsed_s=round(elapsed_s, 3),
                agent_config=self._agent_config,
                llm_model=self._agent_config.get("llm_model") or getattr(cfg, "llm_model", None),
            )
            self._journal.write(record, output.final_state)
        except Exception as exc:
            logger.warning("journal write failed (non-fatal): %s", exc)


def _sanitize_state(state: dict | None) -> dict | None:
    """Pass-through for the journal/FixOutput.

    Provider/hooks no longer live in state (they're in config["configurable"]),
    and budget is already a dict snapshot at this point — see fix() above.
    Kept as a function rather than removed so callers don't change shape; if
    we add a non-serializable state field in the future, sanitize it here.
    """
    if state is None:
        return None
    return dict(state)
