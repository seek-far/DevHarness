"""gitlab_token_check — startup preflight for the GitLab credential.

The worker accepts a personal / project / group access token interchangeably
(same `PRIVATE-TOKEN:` header, same `oauth2:<token>` git credential), so the
code never branches on the kind. What differs is how each one FAILS, and all
the interesting failures are late and misleading without this check: a 401
that reads as "GitLab is down", a 404 that reads as "the repo is gone", and a
Reporter-instead-of-Developer bot that dies at `git push` after the LLM spend.

These tests cover:
  * the four abort cases — empty token, 401, unreachable project, role < Developer
  * every not-decidable case degrades to a warning, never to a failed run
  * kind classification (advisory only — it labels the log line)
  * the capability probe accepting a group token on a nested subgroup, which
    is precisely what username-pattern inference would have rejected
  * that aborting still lets bf_worker write worker:completed:{bug_id}, so a
    misconfigured worker is not restarted into a loop
  * that `gitlab_private_token` stays a DECLARED settings field, which is what
    makes an injected env var beat the image-baked env file
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "bf_worker"))

from services import gitlab_token_check as gtc  # noqa: E402
from services.gitlab_token_check import check_or_abort, classify_kind  # noqa: E402

API = "https://gitlab.example/api/v4"
PROJECT = "42"


# ── fakes ────────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status: int, payload=None, bad_json: bool = False):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


def _cfg(token: str = "glpat-xxx", api: str = API):
    return SimpleNamespace(
        gitlab_api=api, gitlab_private_token=token, env="local_multi_process"
    )


def _routes(*, user=None, project=None, meta=None, project_id=PROJECT):
    """Default = a healthy group token with Developer on the target project."""
    return {
        "/user": user if user is not None else _Resp(200, {"username": "group_7_bot_ab"}),
        f"/projects/{project_id}": project if project is not None else _Resp(
            200, {"permissions": {"project_access": None,
                                  "group_access": {"access_level": 30}}}),
        "/personal_access_tokens/self": meta if meta is not None else _Resp(
            200, {"scopes": ["api", "write_repository"], "expires_at": "2099-01-01"}),
    }


def _install(monkeypatch, routes: dict, *, raise_on: str | None = None):
    """Route GET by URL suffix. `raise_on` makes that one path a network error."""
    seen: list[str] = []

    def fake_get(url, headers=None, timeout=None):
        seen.append(url)
        for suffix, resp in routes.items():
            if url.endswith(suffix):
                if raise_on == suffix:
                    raise requests.ConnectionError("boom")
                return resp
        raise AssertionError(f"unrouted URL: {url}")

    monkeypatch.setattr(gtc.requests, "get", fake_get)
    return seen


# ── the four aborts ──────────────────────────────────────────────────────────


def test_empty_token_aborts_naming_where_it_looked(monkeypatch):
    """An env var that EXISTS but is empty beats the env file, so a
    half-populated Secret lands here rather than falling back."""
    _install(monkeypatch, _routes())
    with pytest.raises(SystemExit) as e:
        check_or_abort(_cfg(token=""), project_id=PROJECT)
    msg = str(e.value)
    assert "GITLAB_PRIVATE_TOKEN is empty" in msg
    assert "worker_local_multi_process.env" in msg
    assert "EMPTY still" in msg, "the empty-env-var trap must be spelled out"


def test_dead_token_aborts_on_401(monkeypatch):
    _install(monkeypatch, _routes(user=_Resp(401)))
    with pytest.raises(SystemExit) as e:
        check_or_abort(_cfg(), project_id=PROJECT)
    assert "401" in str(e.value)
    assert "expiry" in str(e.value)


@pytest.mark.parametrize("status", [403, 404])
def test_unreachable_project_aborts(monkeypatch, status):
    """GitLab answers 404 for projects you may not see, so this is the
    credential-layer twin of the OIDC project_id check."""
    _install(monkeypatch, _routes(project=_Resp(status)))
    with pytest.raises(SystemExit) as e:
        check_or_abort(_cfg(), project_id=PROJECT)
    msg = str(e.value)
    assert PROJECT in msg
    assert "not authorized" in msg
    assert "group access token" in msg, "the message must name the token kind"


def test_role_below_developer_aborts(monkeypatch):
    _install(monkeypatch, _routes(project=_Resp(
        200, {"permissions": {"project_access": {"access_level": 20}}})))
    with pytest.raises(SystemExit) as e:
        check_or_abort(_cfg(), project_id=PROJECT)
    msg = str(e.value)
    assert "Reporter" in msg and "Developer" in msg
    assert "never merges" in msg, "must say Maintainer is not required"


def test_developer_is_enough(monkeypatch):
    _install(monkeypatch, _routes())
    info = check_or_abort(_cfg(), project_id=PROJECT)
    assert info.access_level == 30


# ── everything undecidable degrades to a warning ─────────────────────────────


def test_network_failure_does_not_block_the_run(monkeypatch, caplog):
    _install(monkeypatch, _routes(), raise_on=f"/projects/{PROJECT}")
    info = check_or_abort(_cfg(), project_id=PROJECT)
    assert info is not None
    assert info.access_level is None
    assert "skipping reachability check" in caplog.text


def test_missing_permissions_block_does_not_invent_a_failure(monkeypatch, caplog):
    """Some GitLab versions / visibility settings omit `permissions`. Absent
    evidence is not evidence of a bad role."""
    _install(monkeypatch, _routes(project=_Resp(200, {"name": "proj"})))
    info = check_or_abort(_cfg(), project_id=PROJECT)
    assert info.access_level is None
    assert "no readable `permissions`" in caplog.text


def test_null_access_level_blocks_are_skipped(monkeypatch):
    _install(monkeypatch, _routes(project=_Resp(
        200, {"permissions": {"project_access": None, "group_access": None}})))
    assert check_or_abort(_cfg(), project_id=PROJECT).access_level is None


def test_old_gitlab_without_token_metadata_endpoint(monkeypatch):
    _install(monkeypatch, _routes(meta=_Resp(404)))
    info = check_or_abort(_cfg(), project_id=PROJECT)
    assert info.scopes is None and info.expires_in_days is None


def test_unparseable_user_response_leaves_kind_unknown(monkeypatch):
    _install(monkeypatch, _routes(user=_Resp(200, bad_json=True)))
    assert check_or_abort(_cfg(), project_id=PROJECT).kind == "unknown"


def test_empty_api_url_skips_the_preflight(monkeypatch):
    seen = _install(monkeypatch, _routes())
    assert check_or_abort(_cfg(api=""), project_id=PROJECT) is None
    assert seen == []


def test_missing_project_id_skips_the_preflight(monkeypatch):
    seen = _install(monkeypatch, _routes())
    assert check_or_abort(_cfg(), project_id="") is None
    assert seen == []


def test_skip_env_sends_no_http_at_all(monkeypatch):
    seen = _install(monkeypatch, _routes())
    monkeypatch.setenv("GITLAB_SKIP_TOKEN_CHECK", "1")
    assert check_or_abort(_cfg(), project_id=PROJECT) is None
    assert seen == []


# ── expiry warning ───────────────────────────────────────────────────────────


def test_imminent_expiry_warns_without_blocking(monkeypatch, caplog):
    soon = (date.today() + timedelta(days=3)).isoformat()
    _install(monkeypatch, _routes(meta=_Resp(200, {"scopes": ["api"], "expires_at": soon})))
    info = check_or_abort(_cfg(), project_id=PROJECT)
    assert info.expires_in_days == 3
    assert "expires in 3 day" in caplog.text


def test_distant_expiry_is_quiet(monkeypatch, caplog):
    far = (date.today() + timedelta(days=200)).isoformat()
    _install(monkeypatch, _routes(meta=_Resp(200, {"expires_at": far})))
    check_or_abort(_cfg(), project_id=PROJECT)
    assert "Rotate it" not in caplog.text


def test_unparseable_expiry_is_not_a_warning(monkeypatch):
    _install(monkeypatch, _routes(meta=_Resp(200, {"expires_at": "never"})))
    assert check_or_abort(_cfg(), project_id=PROJECT).expires_in_days is None


# ── kind classification (advisory) ───────────────────────────────────────────


@pytest.mark.parametrize("username,kind", [
    ("project_12_bot_ab3f", "project"),
    ("group_7_bot_cd90", "group"),
    ("lishu", "personal"),
    ("project_manager_jane", "personal"),  # not a bot despite the prefix
    ("", "unknown"),
])
def test_classify_kind(username, kind):
    assert classify_kind(username) == kind


def test_group_token_on_a_nested_subgroup_is_accepted(monkeypatch):
    """The reason authorization is a capability probe and not name inference.

    A group access token on a PARENT group legitimately covers projects in
    nested subgroups, and its bot username only names the top-level group —
    so comparing the id in `group_<id>_bot` against the project would reject
    a perfectly valid production setup.
    """
    _install(monkeypatch, _routes(
        project_id="999",
        user=_Resp(200, {"username": "group_1_bot_zz"}),
        project=_Resp(200, {"permissions": {"group_access": {"access_level": 40}}})))
    info = check_or_abort(_cfg(), project_id="999")
    assert info.kind == "group" and info.access_level == 40


def test_log_line_never_contains_the_token(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _install(monkeypatch, _routes())
    check_or_abort(_cfg(token="glpat-SUPERSECRET"), project_id=PROJECT, bug_id="BUG-1")
    assert "phase_marker phase=gitlab_token_check" in caplog.text
    assert "SUPERSECRET" not in caplog.text


# ── C1: the field must stay declared ─────────────────────────────────────────


def test_gitlab_private_token_is_a_declared_settings_field():
    """Undeclared `extra` fields REVERSE pydantic's env-var-vs-env-file
    priority, and settings/*.env is baked into every image by `COPY settings/`.
    If this field ever goes back to being absorbed by extra="allow", a k8s
    Secret / ECS `secrets:` injection is silently overridden by the image."""
    from settings.worker_settings import WorkerSettings

    assert "gitlab_private_token" in WorkerSettings.model_fields


def test_declared_field_lets_an_injected_env_var_win(monkeypatch, tmp_path):
    """The mechanism behind the test above, pinned against pydantic itself so
    a library upgrade that changes precedence is caught here rather than in
    production as a worker still using the image-baked credential."""
    from pydantic_settings import BaseSettings, SettingsConfigDict

    env_file = tmp_path / "worker.env"
    env_file.write_text("GITLAB_PRIVATE_TOKEN=from_image_env_file\n")

    class _S(BaseSettings):
        gitlab_private_token: str = ""
        model_config = SettingsConfigDict(env_file=env_file, extra="allow")

    monkeypatch.delenv("GITLAB_PRIVATE_TOKEN", raising=False)
    assert _S().gitlab_private_token == "from_image_env_file", (
        "no env var → the env file is still read, so local development is "
        "unaffected by the declaration"
    )

    monkeypatch.setenv("GITLAB_PRIVATE_TOKEN", "from_k8s_secret")
    assert _S().gitlab_private_token == "from_k8s_secret"

    monkeypatch.setenv("GITLAB_PRIVATE_TOKEN", "")
    assert _S().gitlab_private_token == "", (
        "an empty env var wins too — it does NOT fall back to the file, which "
        "is why the empty-token abort exists"
    )


# ── aborting must not start a restart loop ───────────────────────────────────


class _FakeRedis:
    def __init__(self):
        self.calls = []

    async def setex(self, *a, **k):
        self.calls.append("setex")

    async def set(self, *a, **k):
        self.calls.append("set")

    async def delete(self, *a, **k):
        self.calls.append("delete")

    async def aclose(self):
        self.calls.append("aclose")


def test_preflight_abort_still_marks_the_worker_completed(monkeypatch):
    """Otherwise a misconfigured credential becomes MAX_WORKER_RESTARTS worth
    of identical failures: HealthMonitor restarts on an abnormal exit unless
    worker:completed:{bug_id} is set, and a bad token fails deterministically.
    """
    pytest.importorskip("redis", reason="redis client not installed")
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "bf_worker_entrypoint_tokencheck", _ROOT / "bf_worker" / "bf_worker.py")
    W = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(W)

    monkeypatch.setattr(W, "purge_run_records", lambda key: None)

    async def _no_heartbeat(*a, **k):
        await asyncio.sleep(3600)

    monkeypatch.setattr(W, "_heartbeat_loop", _no_heartbeat)

    w = W.BugFixWorker("BUG-1")
    w._redis = _FakeRedis()
    monkeypatch.setattr(w, "_cleanup_repo", lambda: None)
    monkeypatch.setattr(w, "_run_graph", lambda: (_ for _ in ()).throw(
        SystemExit("GitLab credential preflight failed")))

    with pytest.raises(SystemExit):
        asyncio.run(w.run())
    assert "set" in w._redis.calls, (
        "the completion key must be written, or the orchestrator restarts a "
        "worker that will fail the same way every time"
    )
