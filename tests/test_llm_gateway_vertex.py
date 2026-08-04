"""Vertex AI backend: keyless `auth: gcp` + the reasoning-token billing dialect.

Sibling of tests/test_llm_gateway_azure.py. Two things are pinned here that
have real incident/probe provenance rather than being generic coverage:

  * `_expiry_to_epoch` — google.auth hands back a NAIVE datetime documented as
    UTC. Reading it with the ambient timezone is wrong in a way that is
    invisible on a UTC CI box and becomes a 401 storm on a us-* deployment.
    The test forces a non-UTC timezone so the bug cannot hide.

  * `_fold_reasoning` — Vertex reports thinking tokens OUTSIDE
    `completion_tokens`; OpenAI/Azure report them INSIDE. The numbers in
    test_vertex_dialect_* are verbatim from two live gemini-2.5-flash probe
    responses (2026-08-04), not invented.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from llm_gateway.config import GatewayConfigError, load_config
from llm_gateway.cost import Usage, cost_of, extract_usage
from llm_gateway.gcp_auth import (
    DEFAULT_SCOPE,
    GcpAuthError,
    GcpTokenProvider,
    _expiry_to_epoch,
)


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


# ── gcp_auth: the timezone line ──────────────────────────────────────────────


def test_naive_expiry_is_read_as_utc():
    naive = datetime(2030, 1, 1, 12, 0, 0)
    aware = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert _expiry_to_epoch(naive) == aware.timestamp()


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs POSIX tzset")
def test_naive_expiry_ignores_the_ambient_timezone():
    """The test that can actually catch the bug.

    `datetime.timestamp()` on a naive value interprets it in the machine's
    local timezone. On a UTC CI box the wrong implementation passes the test
    above, so force a non-UTC zone: an implementation that forgets to stamp
    tzinfo=utc lands 5 hours off here, which on a us-* host means serving
    tokens that expired hours ago.
    """
    old_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        naive = datetime(2030, 1, 1, 12, 0, 0)
        expected = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        assert _expiry_to_epoch(naive) == expected
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


def test_aware_expiry_is_left_alone():
    aware = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert _expiry_to_epoch(aware) == aware.timestamp()


def test_missing_expiry_reads_as_already_expired():
    """Unknown expiry must cost a refresh, never serve a possibly-stale token."""
    assert _expiry_to_epoch(None) == 0.0


# ── gcp_auth: token provider ─────────────────────────────────────────────────


class _FakeCred:
    """Stands in for a google.auth credential."""

    def __init__(self, token="tok-1", ttl_s=3600):
        self.token = token
        self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
            seconds=ttl_s
        )
        self.refresh_calls = 0

    def refresh(self, _request):
        self.refresh_calls += 1
        self.token = f"{self.token.split('-')[0]}-{self.refresh_calls}"


def _provider_with(cred, monkeypatch):
    p = GcpTokenProvider()
    monkeypatch.setattr(p, "_credential", lambda scope: cred)
    # _mint imports google.auth.transport.requests; stub the whole mint so the
    # test never needs the real package.
    def _mint(scope):
        cred.refresh(None)
        from llm_gateway.gcp_auth import _CachedToken

        return _CachedToken(token=cred.token, expires_on=_expiry_to_epoch(cred.expiry))

    monkeypatch.setattr(p, "_mint", _mint)
    return p


def test_token_is_cached_across_calls(monkeypatch):
    cred = _FakeCred()
    p = _provider_with(cred, monkeypatch)

    first = asyncio.run(p.get_token())
    second = asyncio.run(p.get_token())

    assert first == second
    assert cred.refresh_calls == 1


def test_expiring_token_is_refreshed(monkeypatch):
    # Inside the 300s refresh skew → must not be handed out again.
    cred = _FakeCred(ttl_s=60)
    p = _provider_with(cred, monkeypatch)

    asyncio.run(p.get_token())
    asyncio.run(p.get_token())

    assert cred.refresh_calls == 2


def test_concurrent_cold_start_mints_once(monkeypatch):
    """Without the re-check under the lock, a burst mints one token per
    in-flight request."""
    cred = _FakeCred()
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
    of `gcloud auth application-default login`. The error must say so."""
    p = GcpTokenProvider()

    def _mint(scope):
        raise RuntimeError("could not automatically determine credentials")

    monkeypatch.setattr(p, "_mint", _mint)

    with pytest.raises(GcpAuthError, match="application-default"):
        asyncio.run(p.get_token())


def test_empty_token_is_rejected(monkeypatch):
    """An empty bearer would go out as `Authorization: Bearer ` and come back
    401 from Vertex with nothing pointing at the real cause."""
    p = GcpTokenProvider()
    from llm_gateway.gcp_auth import _CachedToken

    monkeypatch.setattr(
        p, "_mint", lambda scope: _CachedToken(token="", expires_on=time.time() + 3600)
    )
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
