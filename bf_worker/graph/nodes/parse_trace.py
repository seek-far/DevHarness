"""
Node: parse_trace
Parses the raw trace text and extracts error_info + suspect file path.

Behavior:
  - Empty / whitespace-only trace → ValueError (run aborts at agent boundary).
  - Trace present but parser cannot extract a structured error/path → fall back
    to raw-trace mode: error_info=<truncated tail of raw trace>,
    suspect_file_path="", parse_trace_fallback=True. The graph routes this
    state directly to react_loop, skipping fetch_source_file, and the LLM is
    given the raw trace plus instructions to use fetch_additional_file to
    explore.
"""

from __future__ import annotations
import logging

from graph.state import BugFixState
from services.parse_trace import parse_trace as _parse_trace

logger = logging.getLogger(__name__)

# Cap on how much of the raw trace we forward into the LLM prompt when the
# parser cannot extract structured info. pytest prints the actionable failure
# at the bottom, so we keep the tail. Larger than the retry-feedback cap
# (4000) because in fallback mode this trace is the LLM's only error context.
_RAW_TRACE_TAIL = 8000


def parse_trace(state: BugFixState) -> BugFixState:
    trace = state["trace"]

    if not trace or not trace.strip():
        raise ValueError("trace is empty — nothing to analyse")

    result = _parse_trace(trace)

    if result["error_message"] is None or not result["suspect_files"]:
        # Fallback: hand the raw trace to the LLM and let it explore.
        if len(trace) > _RAW_TRACE_TAIL:
            error_info = "...[head truncated]\n" + trace[-_RAW_TRACE_TAIL:]
        else:
            error_info = trace
        logger.warning(
            "parse_trace: parser found no structured error/path; "
            "falling back to raw-trace mode (trace=%d chars, forwarded=%d chars)",
            len(trace), len(error_info),
        )
        return {
            "error_info": error_info,
            "suspect_file_path": "",
            "parse_trace_fallback": True,
        }

    file_path = result["suspect_files"][0]["file_path"]
    logger.info("parsed trace: error in %s", file_path)

    return {
        "error_info": result["error_message"],
        "suspect_file_path": file_path,
        "parse_trace_fallback": False,
    }
