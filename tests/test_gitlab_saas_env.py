"""Tests for the additive `gitlab_saas` env (gitlab.com over HTTPS + token).

Pins:
  * gitlab_saas → NO host rewrite (unlike local_* envs); clone uses
    https://oauth2:<token>@host/path (GitLab-recommended PAT form).
  * Regression: local_multi_process behaviour is byte-identical — host
    rewrite gitlab.local→localhost:8080 and http://user:token@ clone auth
    unchanged. This is the "do not affect non-devstack envs" guarantee.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

import providers.gitlab_provider as gp  # noqa: E402
from providers.gitlab_provider import Repo  # noqa: E402


def _patch_cfg(monkeypatch, **kw):
    ns = SimpleNamespace(
        env=kw["env"],
        gitlab_private_token=kw.get("token", "TESTTOKEN"),
        gitlab_username=kw.get("username", "ciuser"),
    )
    monkeypatch.setattr(gp, "cfg", ns)
    return ns


def _capture_clone(monkeypatch, repo: Repo) -> list:
    calls: list = []
    monkeypatch.setattr(Repo, "run", lambda self, *a, **k: calls.append(a) or "")
    repo.ensure_repo_ready()
    return [a for a in calls if a and a[0] == "clone"]


# ── gitlab_saas: no rewrite + https oauth2 token ─────────────────────────────


def test_gitlab_saas_no_host_rewrite(monkeypatch):
    _patch_cfg(monkeypatch, env="gitlab_saas", token="glpat-xxx")
    r = Repo(repo_path="/tmp/x", repo_url="https://gitlab.com/grp/proj")
    assert r.repo_url == "https://gitlab.com/grp/proj"  # untouched


def test_gitlab_saas_clone_uses_oauth2_token(monkeypatch, tmp_path):
    _patch_cfg(monkeypatch, env="gitlab_saas", token="glpat-SECRET")
    r = Repo(repo_path=str(tmp_path / "repo"), repo_url="https://gitlab.com/grp/proj")
    clones = _capture_clone(monkeypatch, r)
    assert clones == [("clone", "https://oauth2:glpat-SECRET@gitlab.com/grp/proj", ".")]


# ── regression: local_multi_process unchanged ────────────────────────────────


def test_local_multi_process_rewrite_unchanged(monkeypatch):
    _patch_cfg(monkeypatch, env="local_multi_process", token="t")
    r = Repo(repo_path="/tmp/x", repo_url="http://gitlab.local/grp/proj")
    assert r.repo_url == "http://localhost:8080/grp/proj"  # legacy rewrite intact


def test_local_multi_process_clone_auth_unchanged(monkeypatch, tmp_path):
    _patch_cfg(monkeypatch, env="local_multi_process", token="tok", username="alice")
    r = Repo(repo_path=str(tmp_path / "repo"), repo_url="http://gitlab.local/grp/proj")
    clones = _capture_clone(monkeypatch, r)
    assert clones == [("clone", "http://alice:tok@localhost:8080/grp/proj", ".")]
