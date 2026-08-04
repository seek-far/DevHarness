"""Vertex AI backend: keyless `auth: gcp` + the reasoning-token billing dialect.

Sibling of tests/test_llm_gateway_azure.py. Two things are pinned here that
have real incident/probe provenance rather than being generic coverage:

  * `_fold_reasoning` — Vertex reports thinking tokens OUTSIDE
    `completion_tokens`; OpenAI/Azure report them INSIDE. The numbers in
    test_vertex_dialect_* are verbatim from live gemini-2.5-flash probe
    responses (2026-08-04), not invented.

  * **The boundary with google.auth.** gcp_auth.py deliberately owns no token
    lifecycle: the library's `Credentials.valid` already refreshes ahead of
    expiry and compares naive-UTC to naive-UTC. An earlier version reimplemented
    that with an epoch conversion and got the timezone wrong. What the library
    does NOT do is lock, so the remaining tests aim at concurrency — plus two
    guards that fail if either upstream assumption stops holding.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from llm_gateway.config import GatewayConfigError, load_config
from llm_gateway.cost import Usage, cost_of, extract_usage
from llm_gateway.gcp_auth import DEFAULT_SCOPE, GcpAuthError, GcpTokenProvider


def _write(tmp_path: Path, cfg: dict) -> Path:
    p = tmp_path / "gw.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _base(**backend_extra) -> dict:
    backend = {
        "name": "b1",
        "base_url": (
            "https://us-central1-aiplatform.googleapis.com/v1/projects/p/"
            "locations/us-central1/endpoints/openapi"
        ),
        "model": "google/gemini-2.5-flash",
    }
    backend.update(backend_extra)
    return {
        "backends": [backend],
        "inference_policy": {"type": "ordered", "order": ["b1"]},
    }


# ── config: gcp auth ─────────────────────────────────────────────────────────


def test_gcp_backend_carries_no_static_key(tmp_path: Path):
    cfg = load_config(_write(tmp_path, _base(auth="gcp")))
    b = cfg.backends[0]
    assert b.auth == "gcp"
    # Empty, NOT the "EMPTY" sentinel — that sentinel means "self-hosted, no
    # auth". Sending it as a bearer token to Vertex is a 401 nobody can read.
    assert b.api_key == ""
    assert b.gcp_scope == DEFAULT_SCOPE


def test_gcp_plus_api_key_is_rejected(tmp_path: Path):
    with pytest.raises(GatewayConfigError, match="keyless"):
        load_config(_write(tmp_path, _base(auth="gcp", api_key="sk-x")))


def test_gcp_plus_api_key_env_is_rejected(tmp_path: Path):
    with pytest.raises(GatewayConfigError, match="keyless"):
        load_config(_write(tmp_path, _base(auth="gcp", api_key_env="SOME_VAR")))


def test_custom_gcp_scope_is_carried(tmp_path: Path):
    scope = "https://www.googleapis.com/auth/cloud-platform.read-only"
    cfg = load_config(_write(tmp_path, _base(auth="gcp", gcp_scope=scope)))
    assert cfg.backends[0].gcp_scope == scope


def test_api_key_backend_still_gets_a_gcp_scope_default(tmp_path: Path):
    """The new field must not become a required key for every other config."""
    cfg = load_config(_write(tmp_path, _base(api_key="sk-x")))
    assert cfg.backends[0].auth == "api_key"
    assert cfg.backends[0].gcp_scope == DEFAULT_SCOPE


def test_vertex_uses_the_chat_profile_not_reasoning(tmp_path: Path):
    """Gemini thinks, but unlike the o-series / gpt-5 family it ACCEPTS
    `temperature`. Pinning it to the chat profile is what keeps temperature=0
    — evaluation's determinism anchor — working on this backend."""
    cfg = load_config(_write(tmp_path, _base(auth="gcp")))
    assert cfg.backends[0].param_profile == "chat"


# ── cost: the two reasoning-token dialects ───────────────────────────────────


def _usage_body(prompt, completion, reasoning, total):
    return {
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "completion_tokens_details": {"reasoning_tokens": reasoning},
            "total_tokens": total,
        }
    }


def test_vertex_dialect_folds_reasoning_into_billable_output():
    """Live gemini-2.5-flash probe #1: 8 + 10 + 27 == 45.

    The identity proves reasoning is charged ON TOP of completion_tokens, so
    billable output is 37, not the 10 the backend put in completion_tokens.
    """
    u = extract_usage(_usage_body(8, 10, 27, 45))
    assert u == Usage(
        prompt_tokens=8, completion_tokens=37, cached_tokens=0, reasoning_tokens=27
    )


def test_vertex_dialect_second_probe():
    """Probe #2 (tool_choice=required): 21 + 8 + 66 == 95.

    Here the visible completion is 8 and the real output is 74 — recording
    completion_tokens verbatim would bill 11% of what was actually spent.
    """
    u = extract_usage(_usage_body(21, 8, 66, 95))
    assert u.completion_tokens == 74
    assert u.reasoning_tokens == 66


def test_openai_dialect_does_not_double_count_reasoning():
    """OpenAI/Azure: total == prompt + completion, because completion ALREADY
    includes the reasoning tokens. Folding here would bill them twice."""
    u = extract_usage(_usage_body(100, 50, 30, 150))
    assert u.completion_tokens == 50
    assert u.reasoning_tokens == 30


def test_missing_total_falls_back_to_the_conservative_reading():
    body = {
        "usage": {
            "prompt_tokens": 8,
            "completion_tokens": 10,
            "completion_tokens_details": {"reasoning_tokens": 27},
        }
    }
    assert extract_usage(body).completion_tokens == 10


def test_incoherent_total_falls_back_to_the_conservative_reading():
    """A total that matches neither dialect means we do not understand this
    backend. Under-report rather than invent a charge."""
    assert extract_usage(_usage_body(8, 10, 27, 999)).completion_tokens == 10


def test_zero_reasoning_is_a_strict_noop():
    u = extract_usage(_usage_body(10, 20, 0, 30))
    assert u.completion_tokens == 20
    assert u.reasoning_tokens == 0


def test_backend_without_reasoning_details_is_unchanged():
    """Every pre-existing backend omits completion_tokens_details entirely."""
    u = extract_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 20}})
    assert u == Usage(
        prompt_tokens=10, completion_tokens=20, cached_tokens=0, reasoning_tokens=0
    )


def test_usage_absent_is_none_not_zero():
    assert extract_usage({"choices": []}) is None


def test_thinking_tokens_actually_reach_the_bill(tmp_path: Path):
    """End-to-end of the fix: the folded count is what cost_of prices."""
    cfg = load_config(
        _write(
            tmp_path,
            _base(
                auth="gcp",
                pricing={"input_per_1m": 0.30, "output_per_1m": 2.50},
            ),
        )
    )
    backend = cfg.backends[0]
    usage = extract_usage(_usage_body(21, 8, 66, 95))

    cost = cost_of(backend, usage)
    expected = 21 * 0.30 / 1e6 + 74 * 2.50 / 1e6
    assert cost == pytest.approx(expected)

    # And the size of the bug being fixed: pricing the raw completion_tokens
    # would have reported a small fraction of the real output charge.
    naive = cost_of(backend, Usage(prompt_tokens=21, completion_tokens=8))
    assert naive < cost / 2


# ── gcp_auth: what google.auth owns, and what it does NOT ────────────────────
#
# The provider used to cache tokens itself, converting credentials.expiry to
# epoch seconds. That conversion was the bug: expiry is a NAIVE datetime
# documented as UTC, and datetime.timestamp() reads naive values in LOCAL time,
# so the result was off by the UTC offset — over-refreshing on UTC+N, serving
# EXPIRED tokens on any us-* host. google.auth answers the same question with
# naive-UTC on both sides, so deleting our arithmetic deleted the bug class.
#
# These tests therefore aim at the seam that is still ours: concurrency.


def test_google_auth_owns_the_expiry_threshold():
    """Pins the assumption this module is built on.

    gcp_auth.py has no skew arithmetic of its own because google.auth already
    refreshes early and compares naive-UTC to naive-UTC. If either stops being
    true, the deletion was wrong and this should fail loudly rather than
    silently reintroducing stale-token risk.
    """
    from google.auth import _helpers

    assert _helpers.REFRESH_THRESHOLD.total_seconds() >= 60, (
        "google.auth no longer refreshes ahead of expiry; gcp_auth.py would "
        "need its own skew again"
    )
    assert _helpers.utcnow().tzinfo is None, (
        "google.auth switched to aware datetimes; re-check Credentials.expired"
    )


def test_credentials_module_has_no_refresh_lock():
    """Scope note, because the obvious phrasing of this test would be a lie.

    google.auth DOES contain locking — `transport/_aiohttp_requests.py` holds an
    asyncio.Lock and `_refresh_worker.RefreshThreadManager` a threading.Lock.
    What has none is the credentials object itself and the **sync**
    AuthorizedSession, which is what google-cloud-bigquery rides. That is the
    evidence that concurrent refresh is merely wasteful rather than incorrect;
    GcpTokenProvider's single-flight is a peak-shaver for the async case, not a
    correctness fix. This assertion covers only the module named in the test.
    """
    import inspect

    from google.auth import credentials as gac

    src = inspect.getsource(gac)
    assert "Lock" not in src, (
        "google.auth added locking — GcpTokenProvider's single-flight may now "
        "be redundant"
    )


# ── gcp_auth: token provider ─────────────────────────────────────────────────


class _FakeCred:
    """Stands in for a google.auth credential, including its `valid` contract."""

    def __init__(self, valid=False):
        self.token = "tok-0" if valid else None
        self._valid = valid
        self.refresh_calls = 0

    @property
    def valid(self):
        return self._valid

    def refresh(self, _request):
        self.refresh_calls += 1
        self.token = f"tok-{self.refresh_calls}"
        self._valid = True

    def expire(self):
        self._valid = False


def _provider_with(cred, monkeypatch):
    p = GcpTokenProvider()
    # Seed the credential the way _credential() would, without touching ADC.
    p._credentials[DEFAULT_SCOPE] = cred
    monkeypatch.setattr(p, "_credential", lambda scope: cred)
    return p


def test_live_credential_short_circuits_without_refreshing(monkeypatch):
    """The fast path: google.auth says the token is still good, so we neither
    lock nor hop to a thread."""
    cred = _FakeCred(valid=True)
    p = _provider_with(cred, monkeypatch)

    assert asyncio.run(p.get_token()) == "tok-0"
    assert cred.refresh_calls == 0


def test_expired_credential_is_refreshed(monkeypatch):
    cred = _FakeCred(valid=False)
    p = _provider_with(cred, monkeypatch)

    assert asyncio.run(p.get_token()) == "tok-1"
    assert cred.refresh_calls == 1


def test_token_is_reused_until_the_library_says_otherwise(monkeypatch):
    cred = _FakeCred(valid=False)
    p = _provider_with(cred, monkeypatch)

    first = asyncio.run(p.get_token())
    second = asyncio.run(p.get_token())      # now valid → no second refresh
    assert first == second == "tok-1"
    assert cred.refresh_calls == 1

    cred.expire()
    assert asyncio.run(p.get_token()) == "tok-2"
    assert cred.refresh_calls == 2


def test_concurrent_cold_start_mints_once(monkeypatch):
    """google.auth has no lock, so this single-flight is ours to provide:
    without the re-check under the lock, a burst mints one token per in-flight
    request."""
    cred = _FakeCred(valid=False)
    p = _provider_with(cred, monkeypatch)

    async def _burst():
        return await asyncio.gather(*(p.get_token() for _ in range(8)))

    tokens = asyncio.run(_burst())
    assert len(set(tokens)) == 1
    assert cred.refresh_calls == 1


def test_missing_google_auth_package_is_a_named_error(monkeypatch):
    p = GcpTokenProvider()

    def _boom(name, *a, **kw):
        if name.startswith("google.auth"):
            raise ImportError("no google.auth")
        return __import__(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", _boom)
    with pytest.raises(GcpAuthError, match="google-auth"):
        p._credential(DEFAULT_SCOPE)


def test_refresh_failure_points_at_adc(monkeypatch):
    """The single most likely operator mistake is `gcloud auth login` instead
    of `gcloud auth application-default login`. google.auth's own
    DefaultCredentialsError does not draw that distinction, so we must."""
    p = GcpTokenProvider()
    monkeypatch.setattr(
        p, "_mint",
        lambda scope: (_ for _ in ()).throw(
            RuntimeError("could not automatically determine credentials")
        ),
    )

    with pytest.raises(GcpAuthError, match="application-default"):
        asyncio.run(p.get_token())


def test_empty_token_is_rejected(monkeypatch):
    """An empty bearer would go out as `Authorization: Bearer ` and come back
    401 from Vertex with nothing pointing at the real cause."""
    p = GcpTokenProvider()
    monkeypatch.setattr(p, "_mint", lambda scope: "")
    with pytest.raises(GcpAuthError, match="empty token"):
        asyncio.run(p.get_token())


# ── app wiring ───────────────────────────────────────────────────────────────


def test_bearer_for_routes_gcp_to_the_gcp_provider(tmp_path: Path, monkeypatch):
    from llm_gateway import app as gw_app

    cfg = load_config(_write(tmp_path, _base(auth="gcp")))

    class _P:
        def __init__(self):
            self.scopes = []

        async def get_token(self, scope):
            self.scopes.append(scope)
            return "gcp-token"

    provider = _P()
    monkeypatch.setitem(gw_app._state, "gcp", provider)

    bearer = asyncio.run(gw_app._bearer_for(cfg.backends[0]))

    assert bearer == "gcp-token"
    assert provider.scopes == [DEFAULT_SCOPE]


def test_bearer_for_static_key_backend_is_untouched(tmp_path: Path):
    from llm_gateway import app as gw_app

    cfg = load_config(_write(tmp_path, _base(api_key="sk-x")))
    assert asyncio.run(gw_app._bearer_for(cfg.backends[0])) == "sk-x"
