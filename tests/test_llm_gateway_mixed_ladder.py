"""A fallback ladder may cross providers. Each rung keeps its own everything.

Ladder under test (mirrors configs/llm_gateway/azure_plus_qwen3.yaml):

    rung 0   azure-gpt-5-mini   auth=entra     param_profile=reasoning   priced
    rung 1   qwen3-dashscope    auth=api_key   param_profile=chat        unpriced

The worker sends ONE canonical request shape — it always includes `temperature=0`,
because it cannot know which backend the policy will select; that decision happens
after the request leaves. The gateway then absorbs the divergence: it strips
`temperature` for the Azure rung (which 400s on it) and passes it through for the
Qwen3 rung (which wants it, and for which it is the determinism anchor).

That asymmetry — same request in, different requests out — is the concrete
argument for having a gateway at all, and it is what these tests pin.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from llm_gateway import app as gateway_app_mod
from llm_gateway.app import _init_app

_BODY: dict[str, Any] = {
    "id": "chatcmpl-x",
    "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100},
}


class _FakeResponse:
    def __init__(self):
        self.status_code = 200
        self.content = json.dumps(_BODY).encode()
        self.headers = {"content-type": "application/json"}


class _RecordingHttp:
    """Captures exactly what the gateway put on the wire, per call."""

    def __init__(self):
        self.calls: list[dict] = []

    async def post(self, url, *, json=None, headers=None, timeout=None, **_kw):
        self.calls.append({"url": url, "body": json, "headers": headers})
        return _FakeResponse()

    async def aclose(self):
        pass


def _config(tmp_path: Path) -> Path:
    cfg = {
        "backends": [
            {
                "name": "azure-gpt-5-mini",
                "base_url": "https://res.openai.azure.com/openai/v1",
                "model": "gpt-5-mini",
                "auth": "entra",
                "param_profile": "reasoning",
                "reasoning_effort": "low",
                "pricing": {"input_per_1m": 0.25, "output_per_1m": 2.00,
                            "cached_input_per_1m": 0.025},
            },
            {
                "name": "qwen3-dashscope",
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "model": "qwen3-coder-480b-a35b-instruct",
                "api_key": "sk-dashscope",
                # auth: api_key (default), param_profile: chat (default),
                # and deliberately NO pricing block.
            },
        ],
        "inference_policy": {
            "type": "ordered",
            "order": ["azure-gpt-5-mini", "qwen3-dashscope"],
            "advance_on": {"fix_failure": True},
        },
    }
    p = tmp_path / "ladder.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


@pytest.fixture
def ladder(tmp_path, monkeypatch):
    """Boot the ladder with a recording upstream and a stubbed Entra provider
    (so no Azure, no `az login`, no network — CI stays hermetic)."""
    app = _init_app(str(_config(tmp_path)))
    client = TestClient(app)
    client.__enter__()

    http = _RecordingHttp()
    gateway_app_mod._state["http"] = http

    class _StubEntra:
        minted = 0

        async def get_token(self, scope, client_id=""):
            type(self).minted += 1
            return "aad-token"

        async def aclose(self):
            pass

    gateway_app_mod._state["entra"] = _StubEntra()
    yield client, http, _StubEntra
    client.__exit__(None, None, None)


def _post(client: TestClient, attempt: int):
    # What the worker actually sends: one canonical shape, temperature included,
    # because it does not know which rung it will land on.
    return client.post(
        "/v1/chat/completions",
        json={"model": "whatever", "temperature": 0, "messages": [{"role": "user", "content": "hi"}]},
        headers={"x-sdlcma-attempt": str(attempt), "x-sdlcma-bug-id": "BUG-1"},
    )


def test_rung0_is_azure_and_rung1_is_qwen3(ladder):
    client, http, _ = ladder

    r0 = _post(client, 0)
    r1 = _post(client, 1)

    assert r0.status_code == 200 and r1.status_code == 200
    assert r0.headers["x-sdlcma-backend-name"] == "azure-gpt-5-mini"
    assert r1.headers["x-sdlcma-backend-name"] == "qwen3-dashscope"
    # attempt beyond the ladder clamps to the last rung rather than erroring.
    assert _post(client, 7).headers["x-sdlcma-backend-name"] == "qwen3-dashscope"


def test_each_rung_gets_its_own_credential(ladder):
    """Keyless on one rung, a static key on the other — in the SAME header."""
    client, http, stub = ladder

    _post(client, 0)
    _post(client, 1)

    assert http.calls[0]["headers"]["authorization"] == "Bearer aad-token"
    assert http.calls[1]["headers"]["authorization"] == "Bearer sk-dashscope"
    assert stub.minted == 1, "only the keyless rung should mint a token"


def test_gateway_absorbs_the_param_divergence(ladder):
    """The heart of it. The worker sent `temperature=0` both times. Azure would
    400 on it; Qwen3 needs it. One request in, two different requests out."""
    client, http, _ = ladder

    _post(client, 0)
    _post(client, 1)

    azure_body, qwen_body = http.calls[0]["body"], http.calls[1]["body"]

    assert "temperature" not in azure_body, "Azure rejects `temperature` — it must be stripped"
    assert azure_body["reasoning_effort"] == "low"

    assert qwen_body["temperature"] == 0, "Qwen3 wants it — it must pass through untouched"
    assert "reasoning_effort" not in qwen_body


def test_each_rung_gets_its_own_model_name(ladder):
    client, http, _ = ladder

    _post(client, 0)
    _post(client, 1)

    assert http.calls[0]["body"]["model"] == "gpt-5-mini"
    assert http.calls[1]["body"]["model"] == "qwen3-coder-480b-a35b-instruct"


def test_priced_rung_reports_cost_unpriced_rung_reports_nothing(ladder):
    """The unpriced rung must return NO cost header — not `0`. A cost of zero
    would tell the worker (and the dashboard) that the call was free."""
    client, _, _ = ladder

    r0 = _post(client, 0)
    r1 = _post(client, 1)

    # 1000 prompt + 100 completion at Azure's rates.
    expected = 1000 * 0.25 / 1e6 + 100 * 2.00 / 1e6
    assert float(r0.headers["x-sdlcma-cost-usd"]) == pytest.approx(expected, rel=1e-3)
    assert "x-sdlcma-cost-usd" not in r1.headers


def test_each_rung_gets_its_own_url(ladder):
    client, http, _ = ladder

    _post(client, 0)
    _post(client, 1)

    assert http.calls[0]["url"] == "https://res.openai.azure.com/openai/v1/chat/completions"
    assert http.calls[1]["url"] == (
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    )
