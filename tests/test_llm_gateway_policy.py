"""llm_gateway — config loader + stateless inference policy.

The policy is a pure function of (attempt, config). No per-bug state, no
backend-error tally, no lock. These tests cover:

  - config: YAML and JSON both parse to the same shape
  - config: missing/duplicate/unknown names raise GatewayConfigError
  - config: api_key_env resolution + missing-env-var error path
  - config: legacy `advance_on.backend_error` field is accepted & ignored
  - policy: ordered, single backend — every attempt returns it
  - policy: advance_on_fix_failure False → always primary
  - policy: advance_on_fix_failure True → attempt N → backend N (clamped)
  - policy: out-of-range attempts clamp to the last backend
  - llm_model_check: skip when llm_via_gateway=True

Wire-level proxy behaviour (httpx forwarding, /v1/chat/completions success
and failure shapes) is covered by hand in the regression smoke (see
tests/TESTING.md).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bf_worker"))

from llm_gateway.config import (  # noqa: E402
    GatewayConfigError,
    load_config,
)
from llm_gateway.policy import InferencePolicy, parse_attempt_header  # noqa: E402
from services import llm_model_check  # noqa: E402
from services.llm_model_check import check_or_abort  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────


def _write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "cfg.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def _write_json(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _minimal_yaml(advance_fix: bool = True) -> str:
    return f"""
backends:
  - name: primary
    base_url: http://primary/v1
    model: primary-model
    api_key: literal-key
  - name: fallback
    base_url: http://fallback/v1
    model: fallback-model
    api_key: EMPTY
inference_policy:
  type: ordered
  order: [primary, fallback]
  advance_on:
    fix_failure: {str(advance_fix).lower()}
"""


# ── config loader ─────────────────────────────────────────────────────────────


def test_config_yaml_round_trip(tmp_path: Path) -> None:
    cfg = load_config(_write_yaml(tmp_path, _minimal_yaml()))
    assert [b.name for b in cfg.backends] == ["primary", "fallback"]
    assert cfg.policy.type == "ordered"
    assert cfg.policy.order == ("primary", "fallback")
    assert cfg.policy.advance_on_fix_failure is True
    assert cfg.backends_by_name["primary"].model == "primary-model"
    assert cfg.backends_by_name["fallback"].api_key == "EMPTY"


def test_config_json_round_trip(tmp_path: Path) -> None:
    data = {
        "backends": [
            {"name": "only", "base_url": "http://x/v1", "model": "m", "api_key": "k"},
        ],
        "inference_policy": {
            "type": "ordered",
            "order": ["only"],
            "advance_on": {"fix_failure": False},
        },
    }
    cfg = load_config(_write_json(tmp_path, data))
    assert len(cfg.backends) == 1
    assert cfg.policy.advance_on_fix_failure is False


def test_config_api_key_env_resolution(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MY_SECRET_KEY", "value-from-env")
    yaml = """
backends:
  - name: b
    base_url: http://b/v1
    model: m
    api_key_env: MY_SECRET_KEY
inference_policy:
  type: ordered
  order: [b]
"""
    cfg = load_config(_write_yaml(tmp_path, yaml))
    assert cfg.backends[0].api_key == "value-from-env"


def test_config_api_key_env_missing_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MY_SECRET_KEY", raising=False)
    yaml = """
backends:
  - name: b
    base_url: http://b/v1
    model: m
    api_key_env: MY_SECRET_KEY
inference_policy:
  type: ordered
  order: [b]
"""
    with pytest.raises(GatewayConfigError, match="MY_SECRET_KEY"):
        load_config(_write_yaml(tmp_path, yaml))


def test_config_both_api_key_and_env_raises(tmp_path: Path) -> None:
    yaml = """
backends:
  - name: b
    base_url: http://b/v1
    model: m
    api_key: literal
    api_key_env: SOMETHING
inference_policy:
  type: ordered
  order: [b]
"""
    with pytest.raises(GatewayConfigError, match="api_key OR api_key_env"):
        load_config(_write_yaml(tmp_path, yaml))


def test_config_duplicate_backend_names_rejected(tmp_path: Path) -> None:
    yaml = """
backends:
  - name: same
    base_url: http://a/v1
    model: m
    api_key: k
  - name: same
    base_url: http://b/v1
    model: m
    api_key: k
inference_policy:
  type: ordered
  order: [same]
"""
    with pytest.raises(GatewayConfigError, match="duplicate"):
        load_config(_write_yaml(tmp_path, yaml))


def test_config_order_references_unknown_backend(tmp_path: Path) -> None:
    yaml = """
backends:
  - name: a
    base_url: http://a/v1
    model: m
    api_key: k
inference_policy:
  type: ordered
  order: [a, ghost]
"""
    with pytest.raises(GatewayConfigError, match="ghost"):
        load_config(_write_yaml(tmp_path, yaml))


def test_config_unsupported_policy_type(tmp_path: Path) -> None:
    yaml = """
backends:
  - name: a
    base_url: http://a/v1
    model: m
    api_key: k
inference_policy:
  type: round_robin
  order: [a]
"""
    with pytest.raises(GatewayConfigError, match="round_robin"):
        load_config(_write_yaml(tmp_path, yaml))


def test_config_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(GatewayConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_config_legacy_backend_error_field_accepted_and_ignored(tmp_path: Path) -> None:
    """Older configs (drafted before the policy went stateless) may still
    carry advance_on.backend_error. Accept it for backward compat — the
    loader logs a one-line note and proceeds. Loading must succeed and
    produce the same shape as without the field."""
    yaml = """
backends:
  - name: a
    base_url: http://a/v1
    model: m
    api_key: k
inference_policy:
  type: ordered
  order: [a]
  advance_on:
    fix_failure: true
    backend_error: true
"""
    cfg = load_config(_write_yaml(tmp_path, yaml))
    assert cfg.policy.advance_on_fix_failure is True
    # The field is consumed but not surfaced — no attribute on the dataclass.
    assert not hasattr(cfg.policy, "advance_on_backend_error")


def test_config_bundled_three_files_parse() -> None:
    # The three configs the user asked us to ship must always parse.
    base = ROOT / "configs" / "llm_gateway"
    # qwen3_api / qwen3_plus_local reference DASHSCOPE_API_KEY; provide a
    # value so the loader doesn't fail on the api_key_env check (the real
    # gateway would do the same in its startup env).
    import os
    os.environ.setdefault("DASHSCOPE_API_KEY", "test-key")
    os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
    for name in ("qwen3_api.yaml", "self_hosted.yaml", "qwen3_plus_local.yaml",
                 "qwen3_and_ds4.yaml"):
        cfg = load_config(base / name)
        assert cfg.policy.type == "ordered"
        assert len(cfg.policy.order) >= 1


# ── policy: parse_attempt_header ──────────────────────────────────────────────


def test_parse_attempt_header_missing() -> None:
    assert parse_attempt_header(None) == 0
    assert parse_attempt_header("") == 0


def test_parse_attempt_header_malformed() -> None:
    assert parse_attempt_header("abc") == 0
    assert parse_attempt_header("-3") == 0  # clamped to 0


def test_parse_attempt_header_valid() -> None:
    assert parse_attempt_header("0") == 0
    assert parse_attempt_header("2") == 2


# ── policy: ordered selection ─────────────────────────────────────────────────


def test_policy_single_backend_always_returns_it(tmp_path: Path) -> None:
    yaml = """
backends:
  - name: only
    base_url: http://x/v1
    model: m
    api_key: k
inference_policy:
  type: ordered
  order: [only]
  advance_on:
    fix_failure: true
"""
    cfg = load_config(_write_yaml(tmp_path, yaml))
    pol = InferencePolicy(cfg)
    for attempt in (0, 1, 5, 99):
        sel = pol.select(attempt)
        assert sel.backend.name == "only"


def test_policy_advance_on_fix_failure_off_pins_primary(tmp_path: Path) -> None:
    cfg = load_config(_write_yaml(tmp_path, _minimal_yaml(advance_fix=False)))
    pol = InferencePolicy(cfg)
    for attempt in (0, 1, 2, 10):
        sel = pol.select(attempt)
        assert sel.backend.name == "primary"
        assert sel.reason == "primary"


def test_policy_advance_on_fix_failure_climbs(tmp_path: Path) -> None:
    cfg = load_config(_write_yaml(tmp_path, _minimal_yaml(advance_fix=True)))
    pol = InferencePolicy(cfg)
    s0 = pol.select(0)
    assert s0.backend.name == "primary"
    assert s0.reason == "primary"
    s1 = pol.select(1)
    assert s1.backend.name == "fallback"
    assert s1.reason == "fix_failure_advance"


def test_policy_attempt_above_order_length_clamps(tmp_path: Path) -> None:
    cfg = load_config(_write_yaml(tmp_path, _minimal_yaml(advance_fix=True)))
    pol = InferencePolicy(cfg)
    # Order has 2 entries; any attempt ≥ 1 lands on the last.
    for attempt in (2, 7, 99):
        assert pol.select(attempt).backend.name == "fallback"


def test_policy_is_pure_function_of_attempt(tmp_path: Path) -> None:
    """Same (cfg, attempt) → same (backend, reason). No hidden state."""
    cfg = load_config(_write_yaml(tmp_path, _minimal_yaml()))
    pol = InferencePolicy(cfg)
    for attempt in (0, 1, 2, 5):
        a = pol.select(attempt)
        b = pol.select(attempt)
        assert (a.backend.name, a.reason) == (b.backend.name, b.reason)


def test_policy_three_backend_ladder(tmp_path: Path) -> None:
    yaml = """
backends:
  - name: a
    base_url: http://a/v1
    model: ma
    api_key: k
  - name: b
    base_url: http://b/v1
    model: mb
    api_key: k
  - name: c
    base_url: http://c/v1
    model: mc
    api_key: k
inference_policy:
  type: ordered
  order: [a, b, c]
  advance_on:
    fix_failure: true
"""
    cfg = load_config(_write_yaml(tmp_path, yaml))
    pol = InferencePolicy(cfg)
    assert pol.select(0).backend.name == "a"
    assert pol.select(1).backend.name == "b"
    assert pol.select(2).backend.name == "c"
    assert pol.select(99).backend.name == "c"  # clamp


# ── llm_model_check integration with llm_via_gateway ──────────────────────────


def test_model_check_skipped_when_via_gateway(monkeypatch) -> None:
    called = {"n": 0}

    def must_not_call(base_url, timeout_s=10.0):
        called["n"] += 1
        return "anything"

    monkeypatch.setattr(llm_model_check, "query_served_model", must_not_call)
    cfg = SimpleNamespace(
        llm_api_key="EMPTY",  # would normally trigger the probe
        llm_api_base_url="http://gateway:9000/v1",
        llm_model="some-model",
        llm_via_gateway=True,
    )
    out = check_or_abort(cfg)
    assert out is None
    assert called["n"] == 0
