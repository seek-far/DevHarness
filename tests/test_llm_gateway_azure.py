"""Gateway-side Azure support: keyless auth, param normalisation, cost metering.

Three properties, none of which need a real Azure resource:

  * `auth: entra` swaps the bearer token's SOURCE, not the HTTP shape. Azure's
    /openai/v1 route wants `Authorization: Bearer …` — the same header a static
    key already produces — so keyless costs zero special-casing.
  * `param_profile: reasoning` strips `temperature`, which the gpt-5 family
    rejects with HTTP 400. Doing this in the gateway is the point of having a
    gateway: the worker emits one canonical request shape and per-backend
    divergence is absorbed here.
  * Cost is metered where it can be known — the gateway is the only component
    that sees both the chosen backend and its price. A cache hit costs nothing
    and must never touch the spend counter.

Backward compatibility is asserted explicitly: a config with none of the new
keys must behave exactly as it did before.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from llm_gateway.config import GatewayConfigError, load_config
from llm_gateway.cost import Usage, cost_of, extract_usage
from llm_gateway.params import apply_param_profile


def _write(tmp_path: Path, cfg: dict) -> Path:
    p = tmp_path / "gw.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _base(**backend_extra) -> dict:
    backend = {
        "name": "b1",
        "base_url": "https://res.openai.azure.com/openai/v1",
        "model": "gpt-5-mini",
    }
    backend.update(backend_extra)
    return {
        "backends": [backend],
        "inference_policy": {"type": "ordered", "order": ["b1"]},
    }


# ── config: backward compatibility ───────────────────────────────────────────


def test_legacy_config_is_unchanged(tmp_path: Path):
    """Every pre-existing config omits all the new keys. They must keep their
    old behaviour exactly: static api_key auth, chat params, no pricing."""
    cfg = load_config(_write(tmp_path, _base(api_key="sk-x")))
    b = cfg.backends[0]
    assert b.auth == "api_key"
    assert b.api_key == "sk-x"
    assert b.param_profile == "chat"
    assert b.pricing is None


# ── config: entra auth ───────────────────────────────────────────────────────


def test_entra_backend_carries_no_static_key(tmp_path: Path):
    cfg = load_config(_write(tmp_path, _base(auth="entra")))
    b = cfg.backends[0]
    assert b.auth == "entra"
    # Empty, NOT the "EMPTY" sentinel — that means "self-hosted, no auth",
    # which is a different thing entirely.
    assert b.api_key == ""
    assert b.entra_scope == "https://cognitiveservices.azure.com/.default"


def test_entra_plus_api_key_is_rejected(tmp_path: Path):
    """One of them is a leftover. Silently preferring either is how a run ends
    up on the wrong credential."""
    with pytest.raises(GatewayConfigError, match="keyless"):
        load_config(_write(tmp_path, _base(auth="entra", api_key="sk-x")))


def test_entra_plus_api_key_env_is_rejected(tmp_path: Path):
    with pytest.raises(GatewayConfigError, match="keyless"):
        load_config(_write(tmp_path, _base(auth="entra", api_key_env="SOME_VAR")))


def test_unknown_auth_mode_is_rejected(tmp_path: Path):
    with pytest.raises(GatewayConfigError, match="auth="):
        load_config(_write(tmp_path, _base(auth="oauth2")))


def test_user_assigned_identity_client_id_is_carried(tmp_path: Path):
    cfg = load_config(_write(tmp_path, _base(auth="entra", entra_client_id="uami-1")))
    assert cfg.backends[0].entra_client_id == "uami-1"


# ── config: param profile ────────────────────────────────────────────────────


def test_unknown_param_profile_is_rejected(tmp_path: Path):
    with pytest.raises(GatewayConfigError, match="param_profile="):
        load_config(_write(tmp_path, _base(api_key="k", param_profile="thinking")))


def test_reasoning_effort_without_reasoning_profile_is_rejected(tmp_path: Path):
    """A `reasoning_effort` on a chat backend is silently inert — which means a
    typo'd profile would look configured but do nothing. Fail loudly instead."""
    with pytest.raises(GatewayConfigError, match="only meaningful"):
        load_config(_write(tmp_path, _base(api_key="k", reasoning_effort="low")))


def test_unknown_reasoning_effort_is_rejected(tmp_path: Path):
    with pytest.raises(GatewayConfigError, match="reasoning_effort="):
        load_config(
            _write(tmp_path, _base(api_key="k", param_profile="reasoning",
                                   reasoning_effort="extreme"))
        )


# ── params: the actual 400 ───────────────────────────────────────────────────


def test_chat_profile_is_a_strict_noop(tmp_path: Path):
    cfg = load_config(_write(tmp_path, _base(api_key="k")))
    body = {"model": "qwen", "temperature": 0, "max_tokens": 100, "messages": []}
    before = dict(body)
    apply_param_profile(body, cfg.backends[0])
    assert body == before


def test_reasoning_profile_drops_temperature(tmp_path: Path):
    """The real 400: Azure answers `unsupported_value` on `temperature` for the
    whole gpt-5 family. It must be ABSENT, not pinned to 1."""
    cfg = load_config(_write(tmp_path, _base(api_key="k", param_profile="reasoning")))
    body = {"model": "gpt-5-mini", "temperature": 0, "messages": []}
    apply_param_profile(body, cfg.backends[0])
    assert "temperature" not in body
    assert body == {"model": "gpt-5-mini", "messages": []}


def test_reasoning_profile_renames_max_tokens(tmp_path: Path):
    cfg = load_config(_write(tmp_path, _base(api_key="k", param_profile="reasoning")))
    body = {"max_tokens": 512, "messages": []}
    apply_param_profile(body, cfg.backends[0])
    assert body["max_completion_tokens"] == 512
    assert "max_tokens" not in body


def test_reasoning_effort_is_injected(tmp_path: Path):
    cfg = load_config(
        _write(tmp_path, _base(api_key="k", param_profile="reasoning",
                               reasoning_effort="low"))
    )
    body = {"messages": []}
    apply_param_profile(body, cfg.backends[0])
    assert body["reasoning_effort"] == "low"


def test_caller_supplied_reasoning_effort_wins(tmp_path: Path):
    cfg = load_config(
        _write(tmp_path, _base(api_key="k", param_profile="reasoning",
                               reasoning_effort="low"))
    )
    body = {"messages": [], "reasoning_effort": "high"}
    apply_param_profile(body, cfg.backends[0])
    assert body["reasoning_effort"] == "high"


# ── cost ─────────────────────────────────────────────────────────────────────


PRICED = {"input_per_1m": 0.25, "output_per_1m": 2.00, "cached_input_per_1m": 0.025}


def test_usage_extraction_reads_cached_tokens():
    body = {
        "usage": {
            "prompt_tokens": 4553,
            "completion_tokens": 2150,
            "prompt_tokens_details": {"cached_tokens": 2816},
        }
    }
    u = extract_usage(body)
    assert u == Usage(prompt_tokens=4553, completion_tokens=2150, cached_tokens=2816)


def test_usage_absent_is_none_not_zero():
    """`None` and `Usage(0,0,0)` mean different things: 'the backend told us
    nothing' vs 'the call genuinely used no tokens'. Conflating them would let
    us report a confident $0.00 for a call we know nothing about."""
    assert extract_usage({"choices": []}) is None
    assert extract_usage("not a dict") is None


def test_cached_tokens_are_discounted_not_double_charged(tmp_path: Path):
    """Cached input is a SUBSET of prompt_tokens. It must be subtracted from the
    full-price portion, not billed on top of it."""
    cfg = load_config(_write(tmp_path, _base(api_key="k", pricing=PRICED)))
    b = cfg.backends[0]

    full = cost_of(b, Usage(prompt_tokens=1000, completion_tokens=0, cached_tokens=0))
    half = cost_of(b, Usage(prompt_tokens=1000, completion_tokens=0, cached_tokens=1000))

    assert full == pytest.approx(1000 * 0.25 / 1e6)
    assert half == pytest.approx(1000 * 0.025 / 1e6)
    assert half < full


def test_real_run_cost(tmp_path: Path):
    """The numbers from the live 2026-07-14 F01 run against gpt-5-mini."""
    cfg = load_config(_write(tmp_path, _base(api_key="k", pricing=PRICED)))
    cost = cost_of(
        cfg.backends[0],
        Usage(prompt_tokens=4553, completion_tokens=2150, cached_tokens=2816),
    )
    expected = (
        (4553 - 2816) * 0.25 / 1e6 + 2816 * 0.025 / 1e6 + 2150 * 2.00 / 1e6
    )
    assert cost == pytest.approx(expected)
    assert cost < 0.01  # half a cent — the whole premise of this track


def test_unpriced_backend_reports_none_not_zero(tmp_path: Path):
    """A fabricated $0.00 in a cost dashboard reads as 'this was free', which is
    a lie. An honest blank is better."""
    cfg = load_config(_write(tmp_path, _base(api_key="k")))
    assert cost_of(cfg.backends[0], Usage(prompt_tokens=100, completion_tokens=10)) is None


def test_missing_cached_price_falls_back_to_full_input_price(tmp_path: Path):
    """Over-charge, never under-charge, when the config is incomplete."""
    cfg = load_config(
        _write(tmp_path, _base(api_key="k",
                               pricing={"input_per_1m": 1.0, "output_per_1m": 2.0}))
    )
    cost = cost_of(cfg.backends[0], Usage(prompt_tokens=1000, cached_tokens=1000))
    assert cost == pytest.approx(1000 * 1.0 / 1e6)
