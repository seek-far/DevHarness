"""llm_model_check — startup-time vLLM /v1/models verification.

The check guards a class of silent-failure bugs in self-hosted setups: an
OpenAI-compatible backend may happily accept a wrong model name and serve
whatever it has loaded, with no error visible to the caller. The
discriminator for "self-hosted" is the project convention
``LLM_API_KEY=="EMPTY"`` (see settings/worker_settings.py:llm_api_key
default and the CLAUDE.md Configuration section).

These tests cover every branch of check_or_abort:
  - cloud backend            → no probe, return None
  - self-hosted + match      → return served name
  - self-hosted + mismatch   → SystemExit (with both names in message)
  - mismatch + override env  → log + return served name
  - probe network failure    → log + return None (don't block the run)
"""

from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from services import llm_model_check  # noqa: E402
from services.llm_model_check import (  # noqa: E402
    check_or_abort,
    is_self_hosted,
    query_served_model,
)


# ── fakes ─────────────────────────────────────────────────────────────────────


def _cfg(api_key: str = "EMPTY",
         base_url: str = "http://127.0.0.1:8000/v1",
         model: str = "Qwen/Qwen2.5-Coder-32B-Instruct"):
    return SimpleNamespace(
        llm_api_key=api_key,
        llm_api_base_url=base_url,
        llm_model=model,
    )


def _stub_query(monkeypatch, served: str | None = None, exc: Exception | None = None):
    """Patch query_served_model on the module so check_or_abort sees the fake.

    `served=None, exc=None` → not stubbed (real fn would run). Pass one of
    the two to control the outcome.
    """
    def fake(base_url, timeout_s=10.0):
        if exc is not None:
            raise exc
        return served

    monkeypatch.setattr(llm_model_check, "query_served_model", fake)


# ── is_self_hosted ───────────────────────────────────────────────────────────


def test_is_self_hosted_when_key_is_empty():
    assert llm_model_check.is_self_hosted(_cfg(api_key="EMPTY")) is True


def test_is_self_hosted_false_for_cloud_key():
    assert llm_model_check.is_self_hosted(_cfg(api_key="sk-xxxxx")) is False


def test_is_self_hosted_false_when_attr_missing():
    """A cfg-like object without llm_api_key should not blow up — treat as
    cloud (the safer default; cloud backends will fail loud on bad keys)."""
    assert llm_model_check.is_self_hosted(SimpleNamespace()) is False


# ── cloud backend: never probes, returns None ────────────────────────────────


def test_cloud_returns_none_without_probing(monkeypatch):
    called = {"n": 0}

    def must_not_call(base_url, timeout_s=10.0):
        called["n"] += 1
        return "anything"

    monkeypatch.setattr(llm_model_check, "query_served_model", must_not_call)
    out = check_or_abort(_cfg(api_key="sk-real", model="gpt-4o"))
    assert out is None
    assert called["n"] == 0


# ── self-hosted happy path ───────────────────────────────────────────────────


def test_self_hosted_match_returns_served(monkeypatch):
    _stub_query(monkeypatch, served="Qwen/Qwen2.5-Coder-32B-Instruct")
    out = check_or_abort(_cfg(model="Qwen/Qwen2.5-Coder-32B-Instruct"))
    assert out == "Qwen/Qwen2.5-Coder-32B-Instruct"


# ── mismatch: aborts unless override ─────────────────────────────────────────


def test_mismatch_raises_systemexit_with_both_names(monkeypatch):
    _stub_query(monkeypatch, served="Qwen/Qwen2.5-Coder-32B-Instruct")
    monkeypatch.delenv("LLM_ALLOW_MODEL_MISMATCH", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        check_or_abort(_cfg(model="qwen2.5-coder-7b-instruct"))
    msg = str(exc_info.value)
    # Both names must be in the error so the operator knows what to fix.
    assert "qwen2.5-coder-7b-instruct" in msg
    assert "Qwen/Qwen2.5-Coder-32B-Instruct" in msg


def test_mismatch_override_bypasses_and_returns_served(monkeypatch):
    _stub_query(monkeypatch, served="Qwen/Qwen2.5-Coder-32B-Instruct")
    monkeypatch.setenv("LLM_ALLOW_MODEL_MISMATCH", "1")
    out = check_or_abort(_cfg(model="qwen2.5-coder-7b-instruct"))
    assert out == "Qwen/Qwen2.5-Coder-32B-Instruct"


def test_mismatch_override_other_values_dont_bypass(monkeypatch):
    """Only the exact string "1" bypasses — "0", "true", "" do not, so users
    can't accidentally disable the check by leaving the var set to junk."""
    _stub_query(monkeypatch, served="A")
    for val in ("0", "true", "yes", ""):
        monkeypatch.setenv("LLM_ALLOW_MODEL_MISMATCH", val)
        with pytest.raises(SystemExit):
            check_or_abort(_cfg(model="B"))


# ── probe failure: log + return None (do not abort the run) ──────────────────


def test_network_failure_returns_none_does_not_abort(monkeypatch, caplog):
    _stub_query(monkeypatch, exc=urllib.error.URLError("Connection refused"))
    with caplog.at_level("WARNING"):
        out = check_or_abort(_cfg())
    assert out is None
    assert any("probe" in r.message for r in caplog.records)


def test_invalid_json_returns_none(monkeypatch):
    _stub_query(monkeypatch, exc=json.JSONDecodeError("bad", "doc", 0))
    out = check_or_abort(_cfg())
    assert out is None


def test_empty_data_returns_none(monkeypatch):
    _stub_query(monkeypatch, exc=ValueError("returned no models"))
    out = check_or_abort(_cfg())
    assert out is None


# ── empty base_url: skip probe (config error, not a network problem) ─────────


def test_empty_base_url_skips_probe(monkeypatch):
    called = {"n": 0}

    def must_not_call(base_url, timeout_s=10.0):
        called["n"] += 1
        return "anything"

    monkeypatch.setattr(llm_model_check, "query_served_model", must_not_call)
    out = check_or_abort(_cfg(base_url=""))
    assert out is None
    assert called["n"] == 0


# ── query_served_model: real urllib parsing path ─────────────────────────────


def test_query_served_model_parses_data_list(monkeypatch):
    """Smoke test the actual HTTP parsing without hitting the network."""
    payload = json.dumps({
        "data": [{"id": "Qwen/Qwen2.5-Coder-32B-Instruct", "object": "model"}]
    }).encode()

    class _FakeResp:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        return _FakeResp(payload)

    monkeypatch.setattr(llm_model_check.urllib.request, "urlopen", fake_urlopen)
    assert query_served_model("http://127.0.0.1:8000/v1") == "Qwen/Qwen2.5-Coder-32B-Instruct"


def test_query_served_model_raises_on_empty_data(monkeypatch):
    payload = json.dumps({"data": []}).encode()

    class _FakeResp:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        return _FakeResp(payload)

    monkeypatch.setattr(llm_model_check.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ValueError, match="no models"):
        query_served_model("http://127.0.0.1:8000/v1")


# ── keyless (Entra ID / Managed Identity) is a CLOUD backend, not self-hosted ──


class _EntraCfg:
    """A worker configured for Azure keyless auth. It never sets LLM_API_KEY —
    there IS no key — so llm_api_key keeps its "EMPTY" default, which is exactly
    the string the self-hosted discriminator looks for."""

    llm_api_key = "EMPTY"          # the default; nobody set it
    llm_auth_mode = "entra"
    llm_api_base_url = "https://res.openai.azure.com/openai/v1"
    llm_model = "gpt-5-mini"
    llm_via_gateway = False


def test_entra_auth_is_not_self_hosted():
    """Without this, an Azure keyless worker is misread as self-hosted and probes
    {base_url}/models unauthenticated. It only survives today because the 401 is
    swallowed as a network blip — a silent misclassification resting on a silent
    failure. Make the probe strict and every Azure keyless run aborts at startup."""
    assert llm_model_check.is_self_hosted(_EntraCfg()) is False


def test_entra_auth_skips_the_model_probe(monkeypatch):
    probed = []
    monkeypatch.setattr(
        llm_model_check, "query_served_model",
        lambda *a, **k: probed.append(a) or "whatever",
    )
    assert llm_model_check.check_or_abort(_EntraCfg()) is None
    assert probed == [], "keyless Azure must not be probed as a self-hosted backend"


def test_api_key_mode_still_uses_the_empty_sentinel():
    """The existing convention must keep working for real self-hosted backends."""

    class _SelfHosted:
        llm_api_key = "EMPTY"
        llm_auth_mode = "api_key"

    assert llm_model_check.is_self_hosted(_SelfHosted()) is True


def test_cfg_without_auth_mode_field_defaults_to_api_key():
    """Settings objects predating llm_auth_mode (and test doubles) must not break."""

    class _Old:
        llm_api_key = "EMPTY"

    assert llm_model_check.is_self_hosted(_Old()) is True
