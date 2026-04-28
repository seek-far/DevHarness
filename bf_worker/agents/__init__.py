"""
Agent abstraction layer.

An Agent is one bug-fix approach. The internal LangGraph state machine is
LangGraphAgent; third-party agents (Aider, SWE-agent, custom approaches) are
plugged in by writing an adapter that translates BugInput -> their format and
back to FixOutput. This is the unit of comparison in evaluation mode.

See: agents.base for BugInput / FixOutput / Agent ABC.
"""

from agents.base import Agent, BugInput, FixOutput, Outcome
from agents.run_record import RunRecord, SCHEMA_VERSION

# LangGraphAgent is intentionally NOT re-exported here — importing it pulls in
# langgraph + the LLM client + settings, which is wasteful for callers that only
# need the BugInput/FixOutput/RunRecord types (e.g. the bench CLI's lightweight
# subcommands). Import it directly: `from agents.langgraph_agent import LangGraphAgent`.

__all__ = ["Agent", "BugInput", "FixOutput", "Outcome", "RunRecord", "SCHEMA_VERSION"]
