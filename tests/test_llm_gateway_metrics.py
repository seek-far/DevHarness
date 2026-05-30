"""LLM Gateway Prometheus metrics — counter wiring + histogram + endpoint.

Pins:
  * Cache hit / miss / disabled paths each bump the right
    {result, mode} label set, and they don't double-count.
  * Upstream wallclock histogram is observed on every forward,
    NOT on cache hits (hits bypass forward entirely).
  * /metrics returns 200 with parseable Prometheus text — no 307
    redirect (the same anti-friction guard the gateway has).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from llm_gateway import app as gateway_app_mod
from llm_gateway.app import _init_app


_FAKE_RESPONSE_BODY: dict[str, Any] = {
    "id": "chatcmpl-fake",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
}


class _FakeResponse:
    def __init__(self, status_code: int = 200, body: dict | None = None):
        self.status_code = status_code
        self.content = json.dumps(body if body is not None else _FAKE_RESPONSE_BODY).encode()
        self.headers = {"content-type": "application/json"}


class _FakeHttp:
    def __init__(self):
        self.post_count = 0

    async def post(self, *_a, **_kw):
        self.post_count += 1
        return _FakeResponse()

    async def aclose(self):
        pass


def _counter_value(name: str, labels: dict | None = None) -> float:
    sample = REGISTRY.get_sample_value(name, labels or {})
    return sample if sample is not None else 0.0


def _histogram_count(name: str, labels: dict | None = None) -> float:
    """Sum of observation count from a histogram's `_count` sample."""
    sample = REGISTRY.get_sample_value(name + "_count", labels or {})
    return sample if sample is not None else 0.0


def _write_config(tmp_path: Path, mode: str, db_path: Path | None = None) -> Path:
    cfg: dict[str, Any] = {
        "backends": [{
            "name": "fake-backend",
            "base_url": "http://upstream.invalid/v1",
            "model": "fake-model",
            "api_key": "EMPTY",
            "request_timeout": 10,
        }],
        "inference_policy": {"type": "ordered", "order": ["fake-backend"]},
    }
    if mode != "disabled":
        cfg["cache"] = {
            "mode": mode,
            "db_path": str(db_path or (tmp_path / "c.db")),
            "log_every_n": 0,
        }
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp_path / "gw.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    return cfg_path


@pytest.fixture
def boot():
    created = []

    def _boot(cfg_path: Path):
        app = _init_app(str(cfg_path))
        client = TestClient(app)
        client.__enter__()
        fake = _FakeHttp()
        gateway_app_mod._state["http"] = fake
        created.append((client, fake))
        return client, fake

    yield _boot
    for client, _ in created:
        try:
            client.__exit__(None, None, None)
        except Exception:
            pass


_BODY = {
    "model": "fake-model",
    "messages": [
        {"role": "system", "content": "You are a Python bug fix agent."},
        {"role": "user", "content": "fix this please"},
    ],
    "temperature": 0.0,
}


# ── cache_lookups counter wiring ────────────────────────────────────────────


def test_disabled_mode_increments_disabled_counter(tmp_path, boot):
    cfg = _write_config(tmp_path, "disabled")
    client, _ = boot(cfg)
    before = _counter_value(
        "sdlcma_llm_cache_lookups_total",
        {"result": "disabled", "mode": "disabled"},
    )
    client.post("/v1/chat/completions", json=_BODY)
    after = _counter_value(
        "sdlcma_llm_cache_lookups_total",
        {"result": "disabled", "mode": "disabled"},
    )
    assert after == before + 1


def test_cache_mode_miss_then_hit_counter_split(tmp_path, boot):
    """First call: cache empty → miss + record. Second call: same body
    → hit. The two counters must NOT alias each other (different
    label sets)."""
    cfg = _write_config(tmp_path, "cache")
    client, _ = boot(cfg)

    miss_before = _counter_value(
        "sdlcma_llm_cache_lookups_total",
        {"result": "miss", "mode": "cache"},
    )
    hit_before = _counter_value(
        "sdlcma_llm_cache_lookups_total",
        {"result": "hit", "mode": "cache"},
    )

    client.post("/v1/chat/completions", json=_BODY)
    client.post("/v1/chat/completions", json=_BODY)

    assert _counter_value(
        "sdlcma_llm_cache_lookups_total",
        {"result": "miss", "mode": "cache"},
    ) == miss_before + 1
    assert _counter_value(
        "sdlcma_llm_cache_lookups_total",
        {"result": "hit", "mode": "cache"},
    ) == hit_before + 1


def test_replay_mode_miss_counts_then_returns_409(tmp_path, boot):
    cfg = _write_config(tmp_path, "replay", db_path=tmp_path / "empty.db")
    client, _ = boot(cfg)
    before = _counter_value(
        "sdlcma_llm_cache_lookups_total",
        {"result": "miss", "mode": "replay"},
    )
    r = client.post("/v1/chat/completions", json=_BODY)
    assert r.status_code == 409
    assert _counter_value(
        "sdlcma_llm_cache_lookups_total",
        {"result": "miss", "mode": "replay"},
    ) == before + 1


def test_record_mode_increments_miss_only_on_write_path(tmp_path, boot):
    """record mode doesn't consult cache on lookup (different from
    cache mode), so it should bump miss ONLY after a successful
    upstream + write. Verify by counting forward calls vs misses."""
    cfg = _write_config(tmp_path, "record")
    client, fake = boot(cfg)
    miss_before = _counter_value(
        "sdlcma_llm_cache_lookups_total",
        {"result": "miss", "mode": "record"},
    )
    client.post("/v1/chat/completions", json=_BODY)
    assert fake.post_count == 1
    assert _counter_value(
        "sdlcma_llm_cache_lookups_total",
        {"result": "miss", "mode": "record"},
    ) == miss_before + 1


# ── upstream wallclock histogram ────────────────────────────────────────────


def test_upstream_histogram_observed_on_forward(tmp_path, boot):
    cfg = _write_config(tmp_path, "disabled")
    client, _ = boot(cfg)
    before = _histogram_count(
        "sdlcma_llm_upstream_wallclock_ms", {"backend": "fake-backend"}
    )
    client.post("/v1/chat/completions", json=_BODY)
    after = _histogram_count(
        "sdlcma_llm_upstream_wallclock_ms", {"backend": "fake-backend"}
    )
    assert after == before + 1


def test_upstream_histogram_NOT_observed_on_cache_hit(tmp_path, boot):
    """Cache hits short-circuit upstream — the histogram count must
    NOT advance on the second call when it served from cache."""
    cfg = _write_config(tmp_path, "cache")
    client, fake = boot(cfg)
    # Prime cache.
    client.post("/v1/chat/completions", json=_BODY)
    after_prime = _histogram_count(
        "sdlcma_llm_upstream_wallclock_ms", {"backend": "fake-backend"}
    )
    # Second call hits cache; upstream must not be touched.
    client.post("/v1/chat/completions", json=_BODY)
    after_hit = _histogram_count(
        "sdlcma_llm_upstream_wallclock_ms", {"backend": "fake-backend"}
    )
    assert after_hit == after_prime
    assert fake.post_count == 1   # confirms cache hit


# ── /metrics endpoint ──────────────────────────────────────────────────────


def test_metrics_endpoint_serves_prometheus_text(tmp_path, boot):
    cfg = _write_config(tmp_path, "cache")
    client, _ = boot(cfg)
    # Drive at least one request so the labels exist.
    client.post("/v1/chat/completions", json=_BODY)

    r = client.get("/metrics", follow_redirects=False)
    assert r.status_code == 200
    body = r.text
    assert "sdlcma_llm_cache_lookups_total" in body
    assert "sdlcma_llm_upstream_wallclock_ms" in body
    assert "# HELP sdlcma_llm_cache_lookups_total" in body


def test_metrics_endpoint_does_not_redirect(tmp_path, boot):
    """Same anti-307 guard as the gateway — mounted /metrics gives
    307 → /metrics/, this should serve 200 directly."""
    cfg = _write_config(tmp_path, "disabled")
    client, _ = boot(cfg)
    r = client.get("/metrics", follow_redirects=False)
    assert r.status_code == 200
    assert "version=" in r.headers.get("content-type", "")
