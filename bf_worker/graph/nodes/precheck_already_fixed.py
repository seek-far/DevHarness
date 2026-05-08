"""
Node: precheck_already_fixed

Cheap pre-clone, pre-LLM probe for the R10 short-circuit. If a merged MR
already exists for any deterministic branch matching `auto/bf/{bug_id}-*`,
the fix has shipped and there's nothing for this run to do.

Why up-front instead of inside create_fix_branch:
  fetch_trace + parse_trace + react_loop's LLM calls are the expensive part
  of a run. Putting the probe here saves all of that on the R10 path. The
  probe itself is a single REST call (no clone, no working tree).

Why prefix match (`auto/bf/{bug_id}-`) instead of exact branch lookup:
  the exact deterministic name needs base_commit, which only comes from a
  clone. Prefix-matching covers the more general semantic — "any earlier
  fix attempt for this bug merged" — without needing a clone. Bug_id is
  anchored with a trailing dash so BUG-1 doesn't false-positive on BUG-12.
"""

from __future__ import annotations
import logging

from graph.state import BugFixState
from typing import Optional
from langchain_core.runnables import RunnableConfig
from services.runtime_context import get_provider

logger = logging.getLogger(__name__)


def precheck_already_fixed(state: BugFixState, config: Optional[RunnableConfig] = None) -> BugFixState:
    provider = get_provider(config)
    bug_id = state["bug_id"]

    # Defensive: not every provider implements R10 (LocalGit / LocalNoGit
    # return None). The shape was added to all three in the same change so
    # this isn't a guarded call — but keep the hasattr check so external
    # provider impls don't break.
    if not hasattr(provider, "find_merged_mr_by_bug_prefix"):
        return {}

    try:
        mr = provider.find_merged_mr_by_bug_prefix(bug_id)
    except Exception as exc:
        # Probe must never block a real run on its own failure. Log and
        # pass through.
        logger.warning("precheck: merged-MR probe failed: %s — proceeding with run", exc)
        return {}

    if mr is None:
        logger.info("precheck: no merged MR for bug=%s — proceeding", bug_id)
        return {}

    logger.info("precheck: R10 hit — merged MR %s; skipping run", mr.get("url"))
    return {
        "already_fixed":  True,
        "review_result":  mr,
        "review_status":  "already_merged",
        "review_url":     mr.get("url"),
        "review_id":      mr.get("id"),
        "review_iid":     mr.get("iid"),
    }
