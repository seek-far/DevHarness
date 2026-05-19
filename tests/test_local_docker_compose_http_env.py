"""Tests for the additive `local_docker_compose_http` env (Stage-1 of the
cloud rehearsal: containerized stack → docker-compose GitLab by container
name, token-over-HTTP, NO SSH).

Pins:
  * local_docker_compose_http → rewrite gitlab.local→gitlab, clone AND push
    over http://<user>:<token>@gitlab/... ; NO ssh_url, NO ensure_origin_ssh
    (the no-SSH model gitlab_saas mirrors over HTTPS).
  * Regression: local_docker_compose (the SSH sibling) and
    local_multi_process / gitlab_saas are byte-identical — the additive env
    must not perturb existing behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

import providers.gitlab_provider as gp  # noqa: E402
from providers.gitlab_provider import Repo  # noqa: E402


def _patch_cfg(monkeypatch, **kw):
    ns = SimpleNamespace(
        env=kw["env"],
        gitlab_private_token=kw.get("token", "TESTTOKEN"),
        gitlab_username=kw.get("username", "ciuser"),
        gitlab_ssh_port=kw.get("ssh_port", "2222"),
    )
    monkeypatch.setattr(gp, "cfg", ns)
    return ns


def _capture_calls(monkeypatch, repo: Repo) -> list:
    calls: list = []
    monkeypatch.setattr(Repo, "run", lambda self, *a, **k: calls.append(a) or "")
    repo.ensure_repo_ready()
    return calls


# ── local_docker_compose_http: gitlab rewrite + token HTTP + NO ssh ──────────


def test_ldch_rewrites_host_to_gitlab(monkeypatch):
    _patch_cfg(monkeypatch, env="local_docker_compose_http", token="t")
    r = Repo(repo_path="/tmp/x", repo_url="http://gitlab.local/grp/proj")
    assert r.repo_url == "http://gitlab/grp/proj"
    # no SSH at all — ssh_url is never set for this env
    assert not hasattr(r, "ssh_url")


def test_ldch_clone_is_token_http_and_no_ssh_origin(monkeypatch, tmp_path):
    _patch_cfg(monkeypatch, env="local_docker_compose_http",
               token="tok", username="alice")
    r = Repo(repo_path=str(tmp_path / "repo"), repo_url="http://gitlab.local/grp/proj")
    calls = _capture_calls(monkeypatch, r)
    clones = [a for a in calls if a and a[0] == "clone"]
    assert clones == [("clone", "http://alice:tok@gitlab/grp/proj", ".")]
    # the SSH sibling does `remote set-url origin ssh://…`; this env must NOT
    assert not any(a[:2] == ("remote", "set-url") for a in calls)


# ── regression: existing envs byte-identical ─────────────────────────────────


def test_local_docker_compose_ssh_sibling_unchanged(monkeypatch, tmp_path):
    _patch_cfg(monkeypatch, env="local_docker_compose", token="tok", username="bob")
    r = Repo(repo_path=str(tmp_path / "repo"), repo_url="http://gitlab.local/grp/proj")
    assert r.repo_url == "http://gitlab/grp/proj"
    assert r.ssh_url == "ssh://git@gitlab:2222/grp/proj.git"
    calls = _capture_calls(monkeypatch, r)
    # clone over http token, THEN origin switched to ssh (unchanged behaviour)
    assert ("clone", "http://bob:tok@gitlab/grp/proj", ".") in calls
    assert any(a[:2] == ("remote", "set-url") for a in calls)


def test_local_multi_process_unchanged(monkeypatch, tmp_path):
    _patch_cfg(monkeypatch, env="local_multi_process", token="tok", username="alice")
    r = Repo(repo_path=str(tmp_path / "repo"), repo_url="http://gitlab.local/grp/proj")
    assert r.repo_url == "http://localhost:8080/grp/proj"
    clones = [a for a in _capture_calls(monkeypatch, r) if a and a[0] == "clone"]
    assert clones == [("clone", "http://alice:tok@localhost:8080/grp/proj", ".")]


def test_gitlab_saas_unchanged(monkeypatch):
    _patch_cfg(monkeypatch, env="gitlab_saas", token="glpat-xxx")
    r = Repo(repo_path="/tmp/x", repo_url="https://gitlab.com/grp/proj")
    assert r.repo_url == "https://gitlab.com/grp/proj"
