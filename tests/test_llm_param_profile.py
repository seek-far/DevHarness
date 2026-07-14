"""Guard: request params the backend rejects must never be sent.

Incident provenance (2026-07-14, Azure gpt-5-mini bring-up): every LLM call
site in this repo hardcoded `temperature=0`. Reasoning models (o-series, the
whole gpt-5 family) REJECT that parameter — Azure answers:

    HTTP 400  unsupported_value  param=temperature
    "Unsupported value: 'temperature' does not support 0 with this model.
     Only the default (1) value is supported."

…so every single call would have 400'd. `llm_param_profile` decides which
shape goes on the wire. `chat` is the legacy behaviour and must stay
byte-identical; `reasoning` omits the parameter entirely (pinning it to 1
would be accepted but pointless).

Also pins that `entra` auth mode puts an Entra token where the API key goes —
which is the whole trick that lets keyless Azure work with no HTTP-layer
special case, since the OpenAI client emits `Authorization: Bearer <api_key>`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from services.llm_client import apply_param_profile, resolve_api_key  # noqa: E402


class _Cfg:
    """Minimal stand-in for WorkerSettings."""

    def __init__(self, **kw):
        self.llm_api_key = "sk-static"
        self.llm_param_profile = "chat"
        self.llm_auth_mode = "api_key"
        self.llm_entra_scope = "https://cognitiveservices.azure.com/.default"
        self.llm_entra_client_id = ""
        self.__dict__.update(kw)


# ── param profile ────────────────────────────────────────────────────────────


def test_chat_profile_keeps_temperature():
    """Legacy path must not change: Dashscope / vLLM / any chat model still
    gets temperature=0, which is what pins determinism for those backends."""
    kwargs = {"model": "qwen3", "temperature": 0}
    apply_param_profile(kwargs, _Cfg(llm_param_profile="chat"))
    assert kwargs["temperature"] == 0


def test_missing_profile_defaults_to_chat():
    """A cfg object predating this field (or a test double) must behave as
    `chat`, not blow up."""
    cfg = _Cfg()
    del cfg.llm_param_profile
    kwargs = {"temperature": 0}
    apply_param_profile(kwargs, cfg)
    assert kwargs["temperature"] == 0


def test_reasoning_profile_drops_temperature_entirely():
    """The real bug. `temperature` must be ABSENT, not set to 1 — Azure only
    accepts the default, and sending it explicitly buys nothing."""
    kwargs = {"model": "gpt-5-mini", "temperature": 0, "timeout": 600}
    apply_param_profile(kwargs, _Cfg(llm_param_profile="reasoning"))
    assert "temperature" not in kwargs
    # everything else survives untouched
    assert kwargs == {"model": "gpt-5-mini", "timeout": 600}


def test_reasoning_profile_is_idempotent():
    kwargs = {"model": "gpt-5-mini"}
    apply_param_profile(kwargs, _Cfg(llm_param_profile="reasoning"))
    apply_param_profile(kwargs, _Cfg(llm_param_profile="reasoning"))
    assert kwargs == {"model": "gpt-5-mini"}


# ── auth mode ────────────────────────────────────────────────────────────────


def test_api_key_mode_returns_static_key():
    assert resolve_api_key(_Cfg(llm_api_key="sk-abc")) == "sk-abc"


def test_entra_mode_returns_token_not_key(monkeypatch):
    """`entra` swaps the key source. The token lands in the api_key slot, which
    the OpenAI client turns into `Authorization: Bearer <token>` — exactly what
    Azure's /openai/v1 route wants."""
    from services import azure_auth

    seen = {}

    def fake_token(scope, client_id=""):
        seen["scope"] = scope
        seen["client_id"] = client_id
        return "aad-token-xyz"

    monkeypatch.setattr(azure_auth, "get_entra_token", fake_token)

    key = resolve_api_key(
        _Cfg(
            llm_auth_mode="entra",
            llm_api_key="sk-should-be-ignored",
            llm_entra_client_id="uami-123",
        )
    )
    assert key == "aad-token-xyz"
    assert seen["scope"] == "https://cognitiveservices.azure.com/.default"
    assert seen["client_id"] == "uami-123"


def test_entra_mode_without_azure_identity_installed(monkeypatch):
    """Non-Azure deployments don't install azure-identity. Asking for `entra`
    anyway must fail LOUDLY with an actionable message, not silently fall back
    to an empty key and 401 later from the model endpoint."""
    from services import azure_auth

    azure_auth.reset_cache()
    monkeypatch.setitem(sys.modules, "azure.identity", None)  # force ImportError

    with pytest.raises(azure_auth.AzureAuthError, match="azure-identity"):
        azure_auth.get_entra_token("https://example/.default")
