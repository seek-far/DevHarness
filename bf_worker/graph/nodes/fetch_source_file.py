"""
Node: fetch_source_file
Fetches the suspect source file content via the provider.

When the parser found a path but the provider can't read it (synthetic path
from a frozen module, file moved/renamed since the trace was captured, path
outside the working tree), we don't crash the run — we set
`source_fetch_failed=True` and let `react_loop` work from the raw trace plus
the parser's hint. The prompt builder branches on this flag.
"""

from __future__ import annotations
import logging

from graph.state import BugFixState

logger = logging.getLogger(__name__)


def fetch_source_file(state: BugFixState) -> BugFixState:
    provider = state["provider"]
    path = state["suspect_file_path"]
    try:
        content = provider.fetch_file(path)
    except Exception as exc:
        # Don't kill the run on a bad parser hint — fall back to the LLM with
        # the raw trace. We keep `suspect_file_path` populated so the prompt
        # can mention the parser's suggestion as a starting hint, and so the
        # journal records what the parser thought was wrong.
        logger.warning(
            "fetch_source_file: provider.fetch_file(%r) raised %s: %s — "
            "falling back to raw-trace mode",
            path, type(exc).__name__, exc,
        )
        return {
            "source_file_content": "",
            "source_fetch_failed": True,
        }

    logger.info("fetched source file: %s (%d chars)", path, len(content))
    return {
        "source_file_content": content,
        "source_fetch_failed": False,
    }
