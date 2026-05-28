"""Tests for tools/trigger_concurrent_pipelines.py.

Mirrors the two list_fixture_projects tests in
test_gitlab_fixture_repos.py — same namespace-scoping rationale (root
admin token under self-hosted Omnibus + the ``owned=true`` pitfall) and
the same backward-compat path. Adds coverage for the new
``--extra-projects`` flag, which replaces the order_be-specific
``--include-order-be`` / ``--order-be-path`` flags by accepting either
bare names (resolved as ``{namespace}/{name}``) or full
``path_with_namespace`` entries.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if not (200 <= self.status_code < 300):
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


# ── list_fixture_projects ───────────────────────────────────────────────────


def test_list_fixture_projects_scopes_by_namespace(monkeypatch):
    """Self-hosted GitLab can return projects from multiple namespaces
    under the same search prefix. When --namespace is passed, list must
    scope to that namespace via path_with_namespace."""
    from tools import trigger_concurrent_pipelines as mod

    fake_payload = [
        {"id": 1, "path": "sdlcma-fix-f01-x", "path_with_namespace": "root/sdlcma-fix-f01-x"},
        {"id": 2, "path": "sdlcma-fix-f02-y", "path_with_namespace": "root/sdlcma-fix-f02-y"},
        {"id": 3, "path": "sdlcma-fix-f01-x", "path_with_namespace": "someone-else/sdlcma-fix-f01-x"},
        {"id": 4, "path": "unrelated-thing", "path_with_namespace": "root/unrelated-thing"},
    ]
    captured: dict = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["params"] = params
        return _FakeResponse(fake_payload)

    monkeypatch.setattr(mod.requests, "get", fake_get)

    out = mod.list_fixture_projects("http://x/api/v4", "tok", "sdlcma-fix-", "root")
    assert [p["path_with_namespace"] for p in out] == [
        "root/sdlcma-fix-f01-x",
        "root/sdlcma-fix-f02-y",
    ]
    # And the query MUST NOT include owned=true (unreliable on self-hosted
    # for the root admin token — same pitfall fixed in gitlab_fixture_repos
    # commit 82908c3).
    assert "owned" not in captured["params"]


def test_list_fixture_projects_without_namespace_falls_back_to_prefix(monkeypatch):
    """Historical no-namespace call shape: still scope by prefix only,
    no owned=true. Keeps gitlab.com workflows byte-identical."""
    from tools import trigger_concurrent_pipelines as mod

    fake_payload = [
        {"id": 1, "path": "sdlcma-fix-f01-x", "path_with_namespace": "a/sdlcma-fix-f01-x"},
        {"id": 2, "path": "sdlcma-fix-f02-y", "path_with_namespace": "b/sdlcma-fix-f02-y"},
        {"id": 3, "path": "unrelated", "path_with_namespace": "a/unrelated"},
    ]
    captured: dict = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["params"] = params
        return _FakeResponse(fake_payload)

    monkeypatch.setattr(mod.requests, "get", fake_get)
    out = mod.list_fixture_projects("http://x/api/v4", "tok", "sdlcma-fix-")
    assert [p["path"] for p in out] == ["sdlcma-fix-f01-x", "sdlcma-fix-f02-y"]
    assert "owned" not in captured["params"]


# ── resolve_extra_projects ──────────────────────────────────────────────────


def test_resolve_extra_projects_bare_names_use_namespace():
    """Replaces --include-order-be / --order-be-path: bare entries get
    the namespace prefix applied so a stress run can mix real-world repos
    with synthetic fixtures without hardcoding any project path."""
    from tools import trigger_concurrent_pipelines as mod

    out = mod.resolve_extra_projects(["order_be", "some_app"], namespace="root")
    assert out == ["root/order_be", "root/some_app"]


def test_resolve_extra_projects_full_paths_passthrough():
    """Slash-bearing entries are taken as-is — supports cross-namespace
    targets where the extra project lives outside --namespace."""
    from tools import trigger_concurrent_pipelines as mod

    out = mod.resolve_extra_projects(
        ["root/order_be", "other-group/their-repo"], namespace="root"
    )
    assert out == ["root/order_be", "other-group/their-repo"]


def test_resolve_extra_projects_mixed_entries():
    from tools import trigger_concurrent_pipelines as mod

    out = mod.resolve_extra_projects(
        ["order_be", "other-group/their-repo", "extra_app"], namespace="root"
    )
    assert out == ["root/order_be", "other-group/their-repo", "root/extra_app"]


def test_resolve_extra_projects_bare_name_without_namespace_errors():
    """A bare name with no namespace can't be resolved — must error loudly
    rather than silently sending a meaningless path to GitLab."""
    from tools import trigger_concurrent_pipelines as mod

    with pytest.raises(SystemExit, match="bare name"):
        mod.resolve_extra_projects(["order_be"], namespace=None)


def test_resolve_extra_projects_empty_list_returns_empty():
    from tools import trigger_concurrent_pipelines as mod

    assert mod.resolve_extra_projects([], namespace="root") == []
    assert mod.resolve_extra_projects(["", "  "], namespace="root") == []
