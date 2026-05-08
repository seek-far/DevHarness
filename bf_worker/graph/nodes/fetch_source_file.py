"""
Node: fetch_source_file
Fetches the suspect source file content via the provider.

Two failure modes are handled here, in order:

  1. Transient I/O (network blip, NFS hiccup, GitLab 503): wrapped via
     `services.transient_retry.with_transient_retry`. Up to 2 retries on
     known-transient classes; permanent errors and exhausted-transient
     errors raise out of the helper.

  2. Permanent / exhausted: caught here so the run still continues with the
     LLM working from the raw trace. We set `source_fetch_failed=True` and
     keep `suspect_file_path` populated so the prompt can mention the
     parser's suggestion as a starting hint. The retry counter records what
     happened *before* the fall-through (0 if the very first attempt failed
     non-transient; N if N transients fired before the final failure).
"""

from __future__ import annotations
import logging

from graph.state import BugFixState
from typing import Optional
from langchain_core.runnables import RunnableConfig
from services.runtime_context import get_provider
from services.transient_retry import (
    DEFAULT_RETRY_DELAYS,
    classify_transient,
    with_transient_retry,
)

logger = logging.getLogger(__name__)


def fetch_source_file(state: BugFixState, config: Optional[RunnableConfig] = None) -> BugFixState:
    provider = get_provider(config)
    path = state["suspect_file_path"]
    try:
        content, retries = with_transient_retry(
            lambda: provider.fetch_file(path),
            op_name="fetch_source_file",
        )
    except Exception as exc:
        # We get here in two cases:
        #   a. permanent error on the very first attempt (FileNotFoundError,
        #      4xx HTTP, …) — `with_transient_retry` re-raises immediately.
        #   b. transient error that exhausted the retry budget.
        # Either way, fall through to source_fetch_failed mode. Best-effort
        # attempt to count the transients that did fire — if the failure was
        # transient, all `len(DEFAULT_RETRY_DELAYS)` slots were used.
        was_transient, _ = classify_transient(exc)
        retries_attempted = len(DEFAULT_RETRY_DELAYS) if was_transient else 0
        logger.warning(
            "fetch_source_file: provider.fetch_file(%r) failed after %d retries "
            "(%s: %s) — falling back to raw-trace mode",
            path, retries_attempted, type(exc).__name__, exc,
        )
        return {
            "source_file_content": "",
            "source_fetch_failed": True,
            "fetch_source_file_retries": retries_attempted,
        }

    logger.info("fetched source file: %s (%d chars, retries=%d)",
                path, len(content), retries)
    return {
        "source_file_content": content,
        "source_fetch_failed": False,
        "fetch_source_file_retries": retries,
    }
