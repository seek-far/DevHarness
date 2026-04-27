"""
LangGraphAgent — wraps the existing LangGraph state machine.

This is one Agent implementation among many. The graph itself, its nodes, and
the provider abstraction are unchanged; this module just adapts them to the
Agent interface and writes a journal entry on completion.

Future per-agent enhancements (memory lookup, multi-hypothesis, edge-case
test generation, ...) will register against hooks invoked from inside the
graph nodes — that hook layer is intentionally not added yet, since adding it
without a concrete enhancement to motivate the extension points would be
premature.
"""

from __future__ import annotations
import logging
from typing import Any

from agents.base import Agent, BugInput, FixOutput, Outcome
from graph.builder import build_graph
from graph.state import BugFixState

logger = logging.getLogger(__name__)


class LangGraphAgent(Agent):
    name = "langgraph"

    def __init__(self, journal: Any = None):
        # Compile the graph once per agent instance.
        self._graph = build_graph()
        self._journal = journal

    def fix(self, bug_input: BugInput) -> FixOutput:
        initial_state: BugFixState = {
            "provider":        bug_input.provider,
            "bug_id":          bug_input.bug_id,
            "project_id":      bug_input.project_id,
            "project_web_url": bug_input.project_web_url,
            "job_id":          bug_input.job_id,
            "llm_retry_count": 0,
            "fix_retry_count": 0,
        }

        logger.info("agent=%s bug=%s starting", self.name, bug_input.bug_id)

        try:
            final_state = self._graph.invoke(initial_state)
        except Exception as exc:
            logger.exception("agent=%s bug=%s crashed", self.name, bug_input.bug_id)
            output = FixOutput(
                outcome="error",
                bug_id=bug_input.bug_id,
                error=str(exc),
            )
            self._maybe_journal(bug_input, output, None)
            return output

        outcome: Outcome = "error" if final_state.get("error") else "fixed"

        output = FixOutput(
            outcome=outcome,
            bug_id=bug_input.bug_id,
            error=final_state.get("error"),
            iterations=int(final_state.get("fix_retry_count") or 0),
            final_state=_sanitize_state(final_state),
        )

        self._maybe_journal(bug_input, output, final_state)
        return output

    def _maybe_journal(self, bug_input: BugInput, output: FixOutput, raw_state: dict | None) -> None:
        if self._journal is None:
            return
        try:
            self._journal.write(self.name, bug_input, output, raw_state)
        except Exception as exc:
            logger.warning("journal write failed (non-fatal): %s", exc)


def _sanitize_state(state: dict | None) -> dict | None:
    """Drop the provider object (not JSON-serializable)."""
    if state is None:
        return None
    return {k: v for k, v in state.items() if k != "provider"}
