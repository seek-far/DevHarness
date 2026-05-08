"""
Tests for precheck_already_fixed — the early R10 short-circuit that runs
before fetch_trace/parse/react_loop, saving the LLM cost on already-shipped
fixes.

Three things we want to lock down:
  1. When the provider returns a merged MR for the bug prefix, the node
     populates already_fixed/review_status correctly. Routing then sends
     the run to END without the rest of the pipeline running.
  2. When the provider returns None, the node passes through cleanly.
  3. Loose-match prefix anchoring: BUG-1 must NOT short-circuit because
     of a merged MR for BUG-12. Defense against substring false-positives.

We don't run the full graph here (that's covered indirectly by the main
idempotency tests). We test the node + its routing function directly.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from graph.nodes.precheck_already_fixed import precheck_already_fixed  # noqa: E402
from graph.routing import route_after_precheck  # noqa: E402


def _provider_returning(mr_or_none):
    """Minimal provider stub with just the precheck method."""
    p = SimpleNamespace()
    p.find_merged_mr_by_bug_prefix = lambda bug_id: mr_or_none
    return p


def _cfg(provider):
    return {"configurable": {"provider": provider}}


# ── precheck_already_fixed ──────────────────────────────────────────────────


def test_precheck_short_circuits_on_merged_mr():
    mr = {
        "id": 42, "iid": 7, "title": "auto-fix bug BUG-X",
        "url": "http://gitlab/mr/7", "state": "merged",
    }
    provider = _provider_returning(mr)
    update = precheck_already_fixed({"bug_id": "BUG-X"}, _cfg(provider))

    assert update["already_fixed"] is True
    assert update["review_status"] == "already_merged"
    assert update["review_url"] == "http://gitlab/mr/7"
    assert update["review_id"] == 42
    assert update["review_iid"] == 7
    assert update["review_result"] is mr


def test_precheck_passes_through_on_no_mr():
    provider = _provider_returning(None)
    update = precheck_already_fixed({"bug_id": "BUG-Y"}, _cfg(provider))

    # Empty update — graph proceeds normally
    assert update == {}


def test_precheck_swallows_provider_exception():
    """Probe failure must not block a real run — log and pass through."""
    p = SimpleNamespace()
    def boom(bug_id):
        raise RuntimeError("simulated GitLab outage")
    p.find_merged_mr_by_bug_prefix = boom

    update = precheck_already_fixed({"bug_id": "BUG-Z"}, _cfg(p))

    assert update == {}   # treated as "no merged MR found"


def test_precheck_no_op_when_provider_lacks_method():
    """Older / external provider impls without the method are tolerated."""
    p = SimpleNamespace()  # no find_merged_mr_by_bug_prefix
    update = precheck_already_fixed({"bug_id": "BUG-Z"}, _cfg(p))
    assert update == {}


# ── route_after_precheck ────────────────────────────────────────────────────


def test_routing_short_circuits_on_already_fixed():
    assert route_after_precheck({"already_fixed": True}) == "already_fixed"


def test_routing_proceeds_on_default():
    assert route_after_precheck({}) == "fetch_trace"
    assert route_after_precheck({"already_fixed": False}) == "fetch_trace"


# ── prefix anchoring (defense against substring false-positives) ────────────


def test_prefix_anchor_rejects_longer_bug_id_substring():
    """The provider impl must anchor with `auto/bf/{bug_id}-` so that
    BUG-1 does NOT match a merged MR for BUG-12. We assert the contract
    here at the GitLabProvider level by exercising the response-filter
    with a synthetic API response."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))
    from providers.gitlab_provider import Repo  # noqa: E402

    # Mock the requests.get response with two MRs: one matches the
    # prefix-with-dash, one is just a substring match. Only the first
    # should be returned.
    class FakeResp:
        status_code = 200
        def json(self):
            return [
                # would-be substring false positive: BUG-12, not BUG-1
                {"id": 99, "iid": 99, "source_branch": "auto/bf/BUG-12-aaaaaaaa",
                 "state": "merged", "title": "fix 12", "web_url": "http://x/99"},
            ]

    import providers.gitlab_provider as gp_mod
    real_get = gp_mod.requests.get
    gp_mod.requests.get = lambda *a, **kw: FakeResp()

    try:
        repo = Repo.__new__(Repo)
        repo.repo_url = "http://gitlab.example/group/project"
        repo.token = "t"
        result = repo.find_merged_mr_by_bug_prefix("BUG-1")
        # No matching MR for BUG-1 — the BUG-12 entry is filtered out by the
        # `sb.startswith("auto/bf/BUG-1-")` check.
        assert result is None
    finally:
        gp_mod.requests.get = real_get


def test_prefix_anchor_accepts_exact_prefix_with_dash():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))
    from providers.gitlab_provider import Repo  # noqa: E402

    class FakeResp:
        status_code = 200
        def json(self):
            return [
                {"id": 7, "iid": 7, "source_branch": "auto/bf/BUG-1-deadbeef",
                 "state": "merged", "title": "fix 1", "web_url": "http://x/7"},
            ]

    import providers.gitlab_provider as gp_mod
    real_get = gp_mod.requests.get
    gp_mod.requests.get = lambda *a, **kw: FakeResp()

    try:
        repo = Repo.__new__(Repo)
        repo.repo_url = "http://gitlab.example/group/project"
        repo.token = "t"
        result = repo.find_merged_mr_by_bug_prefix("BUG-1")
        assert result is not None
        assert result["id"] == 7
        assert result["state"] == "merged"
    finally:
        gp_mod.requests.get = real_get
