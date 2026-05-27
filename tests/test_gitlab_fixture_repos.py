"""Unit tests for the pure-logic helpers in tools/gitlab_fixture_repos.py
and tools/trigger_concurrent_pipelines.py.

API-calling helpers are covered indirectly: any change to project-name
slugification, fixture filtering, or short-id matching would break here
before it reaches gitlab.com.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import re

from tools.gitlab_fixture_repos import discover_fixtures, fixture_to_project_name
from tools.trigger_concurrent_pipelines import filter_fixtures


def _embed_token(repo_url: str, token: str) -> str:
    # Mirror of the auth_url rewrite in gitlab_fixture_repos.push_fixture_to_main.
    # Keep this function in lockstep with the source — if you change the rewrite
    # there, change it here too.
    return re.sub(r"^(https?)://", rf"\1://oauth2:{token}@", repo_url)


def test_fixture_to_project_name_lowercases_and_prefixes():
    assert (
        fixture_to_project_name("sdlcma-fix-", "F01-off-by-one")
        == "sdlcma-fix-f01-off-by-one"
    )


def test_discover_fixtures_skips_dirs_without_source(tmp_path: Path):
    (tmp_path / "F01-x" / "source").mkdir(parents=True)
    (tmp_path / "F02-y" / "source").mkdir(parents=True)
    (tmp_path / "not-a-fixture").mkdir()
    assert [p.name for p in discover_fixtures(tmp_path)] == ["F01-x", "F02-y"]


def test_discover_fixtures_filter_short_id_and_full_name(tmp_path: Path):
    (tmp_path / "F01-x" / "source").mkdir(parents=True)
    (tmp_path / "F02-y" / "source").mkdir(parents=True)
    assert [p.name for p in discover_fixtures(tmp_path, ["F01"])] == ["F01-x"]
    assert [p.name for p in discover_fixtures(tmp_path, ["F01-x"])] == ["F01-x"]
    assert [p.name for p in discover_fixtures(tmp_path, ["F03"])] == []


def test_filter_fixtures_by_short_id():
    projs = [
        {"path": "sdlcma-fix-f01-off-by-one"},
        {"path": "sdlcma-fix-f02-type"},
        {"path": "sdlcma-fix-f10-whitespace"},
    ]
    out = filter_fixtures(projs, ["F01", "F10"], "sdlcma-fix-")
    assert [p["path"] for p in out] == [
        "sdlcma-fix-f01-off-by-one",
        "sdlcma-fix-f10-whitespace",
    ]


def test_filter_fixtures_by_full_lowercase_name():
    projs = [
        {"path": "sdlcma-fix-f01-off-by-one"},
        {"path": "sdlcma-fix-f02-type"},
    ]
    out = filter_fixtures(projs, ["f02-type"], "sdlcma-fix-")
    assert [p["path"] for p in out] == ["sdlcma-fix-f02-type"]


def test_filter_fixtures_none_returns_all():
    projs = [{"path": "sdlcma-fix-f01-x"}, {"path": "sdlcma-fix-f02-y"}]
    assert filter_fixtures(projs, None, "sdlcma-fix-") == projs


def test_push_auth_url_preserves_scheme_https():
    # gitlab.com / TLS-fronted self-hosted: clone URL is https://, token must
    # be embedded as https://oauth2:<token>@...
    assert _embed_token(
        "https://gitlab.com/user/repo.git", "glpat-abc"
    ) == "https://oauth2:glpat-abc@gitlab.com/user/repo.git"


def test_push_auth_url_preserves_scheme_http():
    # Self-hosted GitLab on a custom port without TLS (e.g. minus:8929):
    # clone URL is http://, regex must NOT silently fail — token must be
    # embedded as http://oauth2:<token>@... or git push prompts for creds.
    assert _embed_token(
        "http://minus:8929/root/sdlcma-fix-f01-off-by-one.git", "glpat-xyz"
    ) == "http://oauth2:glpat-xyz@minus:8929/root/sdlcma-fix-f01-off-by-one.git"
