"""
Node: fetch_source_file
Fetches the suspect source file content via the provider.
"""

from __future__ import annotations
import logging

from graph.state import BugFixState

logger = logging.getLogger(__name__)


def fetch_source_file(state: BugFixState) -> BugFixState:
    provider = state["provider"]
    content = provider.fetch_file(state["suspect_file_path"])
    logger.info("fetched source file: %s (%d chars)", state["suspect_file_path"], len(content))
    return {"source_file_content": content}
