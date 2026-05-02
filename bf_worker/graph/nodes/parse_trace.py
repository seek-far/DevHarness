"""
Node: parse_trace
Parses the raw trace text and extracts error_info + suspect file path.
Raises ValueError if nothing useful is found (lets LangGraph surface the error).
"""

from __future__ import annotations
import logging

from graph.state import BugFixState
from services.parse_trace import parse_trace as _parse_trace

logger = logging.getLogger(__name__)


def parse_trace(state: BugFixState) -> BugFixState:
    # print(state["trace"])
    result = _parse_trace(state["trace"])

    if result["error_message"] is None or not result["suspect_files"]:
        raise ValueError("Could not extract error info from trace")

    file_path = result["suspect_files"][0]["file_path"]
    logger.info("parsed trace: error in %s", file_path)

    return {
        "error_info": result["error_message"],
        "suspect_file_path": file_path,
    }
