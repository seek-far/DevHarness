"""
Enhancements layer — pluggable extensions to LangGraphAgent.

Future enhancements (memory lookup, multi-hypothesis fixing, edge-case test
generation, ...) live as subpackages here. Each registers callbacks against
named hook points exposed by the LangGraph nodes; the core graph itself
is never modified.

This is intentionally LangGraphAgent-specific — third-party agents (Aider,
SWE-agent, ...) bring their own internal extension mechanism. The Agent ABC
is the unit of comparison; enhancements are an implementation detail.
"""

from enhancements.hooks import HookRegistry, HookName

__all__ = ["HookRegistry", "HookName"]
