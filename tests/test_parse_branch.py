"""Tests for orchestrator fix-branch recognition (parser.parse_branch).

Regression context: the CI-result feedback loop routes a pipeline webhook to
the waiting worker only when its ref is recognised as an auto-fix branch.
The real running modes name the branch `auto/bf/{bug_id}-{base_commit[:8]}`
(Repo.deterministic_branch_name), but parse_branch only matched the legacy
`auto/bug_{bug_id}-patch_{branch_id}` shape — so real fix-branch CI results
were misclassified as OtherEvent and the worker timed out before opening an MR.

These tests pin BOTH shapes:
  * legacy `auto/bug_..-patch_..`  — still emitted by integration_test.py
    Step E; behaviour MUST stay byte-identical (non-devstack regression).
  * real `auto/bf/{bug_id}-{sha[:8]}` — the shape every GitLab/local-git run
    actually produces.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.parser import parse_branch, parse_message  # noqa: E402
from orchestrator.models import (  # noqa: E402
    BugReportedEvent,
    ValidationStatusEvent,
    OtherEvent,
)

BUG_ID = "2026_05_16-19_14_37_5"
# Post-tail bug_id: orchestrator now appends `_<4 hex>` (urandom) to
# kill the 100 ms decisecond-collision window. Both shapes must
# round-trip — the no-tail shape is still emitted by older integration
# tests and any pre-tail fixtures.
BUG_ID_TAIL = "2026_05_16-19_14_37_5_a4f2"


# ── parse_branch ─────────────────────────────────────────────────────────────


def test_legacy_branch_still_matches():
    """Legacy shape used by integration_test.py Step E — must not regress."""
    m = parse_branch(f"auto/bug_{BUG_ID}-patch_17_50_41_3")
    assert m is not None
    assert m.groups()[0] == BUG_ID


def test_real_fix_branch_matches():
    """auto/bf/{bug_id}-{base_commit[:8]} — the real running-mode shape."""
    m = parse_branch(f"auto/bf/{BUG_ID}-da6734c5")
    assert m is not None
    assert m.groups()[0] == BUG_ID


def test_real_fix_branch_with_random_tail_matches():
    """bug_id now carries a 4-hex urandom tail; the regex must accept it."""
    m = parse_branch(f"auto/bf/{BUG_ID_TAIL}-da6734c5")
    assert m is not None
    assert m.groups()[0] == BUG_ID_TAIL


def test_legacy_branch_with_random_tail_matches():
    m = parse_branch(f"auto/bug_{BUG_ID_TAIL}-patch_17_50_41_3")
    assert m is not None
    assert m.groups()[0] == BUG_ID_TAIL


def test_non_fix_branches_do_not_match():
    for ref in (
        "main",
        "develop",
        "feature/login",
        f"auto/bf/{BUG_ID}-da6734",        # short sha (7 != 8)
        f"auto/bf/{BUG_ID}-da6734c5e",     # long sha (9 != 8)
        f"auto/bf/{BUG_ID}-DA6734C5",      # uppercase, not [0-9a-f]
        f"auto/bf/BUG-XYZ-da6734c5",       # bug_id not the timestamp shape
        f"auto/bf/{BUG_ID}",               # no sha suffix
        f"auto/bf/{BUG_ID}_a4f-da6734c5",  # tail must be exactly 4 hex
        f"auto/bf/{BUG_ID}_a4f2g-da6734c5",  # tail must be [0-9a-f]
    ):
        assert parse_branch(ref) is None, ref


# ── parse_message classification ─────────────────────────────────────────────


def _pipeline(ref: str, status: str) -> bytes:
    return json.dumps(
        {
            "object_kind": "pipeline",
            "object_attributes": {"ref": ref, "status": status},
            "project": {"id": 2, "web_url": "http://gitlab.local/x/y"},
            "builds": [{"id": 99}],
        }
    ).encode()


def test_real_fix_branch_success_is_validation_event():
    """The exact case that was broken in Option 2: fix-branch CI success."""
    ev = parse_message(_pipeline(f"auto/bf/{BUG_ID}-da6734c5", "success"))
    assert isinstance(ev, ValidationStatusEvent)
    assert ev.bug_id == BUG_ID
    assert ev.status == "success"


def test_legacy_fix_branch_success_is_validation_event():
    ev = parse_message(_pipeline(f"auto/bug_{BUG_ID}-patch_17_50_41_3", "success"))
    assert isinstance(ev, ValidationStatusEvent)
    assert ev.bug_id == BUG_ID
    assert ev.status == "success"


def test_main_branch_failure_is_bug_reported_event():
    ev = parse_message(_pipeline("main", "failed"))
    assert isinstance(ev, BugReportedEvent)
    assert ev.source_branch == "main"


def test_non_fix_branch_success_is_other_event():
    ev = parse_message(_pipeline("main", "success"))
    assert isinstance(ev, OtherEvent)


# ── Item 3: non-main feature branches + auto/* loophole ──────────────────────


def test_feature_branch_failure_is_bug_reported_event():
    """Item 3: failed pipeline on a non-main branch must spawn a worker
    AND carry the source branch through, so the worker rebases / MRs
    against that branch instead of main."""
    ev = parse_message(_pipeline("feature/login", "failed"))
    assert isinstance(ev, BugReportedEvent)
    assert ev.source_branch == "feature/login"


def test_unknown_auto_ref_failure_is_other_event():
    """Item 3a: any ref under `auto/` is the worker's namespace — even a
    failed pipeline on an unrecognised auto-* ref must NOT trigger a
    bug-fix run (would recurse into the worker's own outputs)."""
    ev = parse_message(_pipeline("auto/something-unrelated", "failed"))
    assert isinstance(ev, OtherEvent)


def test_real_fix_branch_with_random_tail_routes_validation():
    """bug_id-with-tail round-trip on the validation path."""
    ev = parse_message(_pipeline(f"auto/bf/{BUG_ID_TAIL}-da6734c5", "success"))
    assert isinstance(ev, ValidationStatusEvent)
    assert ev.bug_id == BUG_ID_TAIL
