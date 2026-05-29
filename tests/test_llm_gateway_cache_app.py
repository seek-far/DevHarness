"""End-to-end wire tests for the gateway cache modes.

Boots a real FastAPI app via TestClient, swaps the upstream httpx client
for a fake that returns a deterministic JSON body, then exercises each
of the four cache modes (disabled / record / replay / cache) plus the
`/cache/stats` endpoint and the response headers (`x-sdlcma-cache`,
`x-sdlcma-cache-key`, `x-sdlcma-backend-name`).

Why TestClient and not pure unit tests of the handler: the cache hot
path is short, but the wiring around it (model rewrite happens INSIDE
_forward, cache key derived from the WORKER's body BEFORE the rewrite,
modes branch in two places — pre-forward lookup + post-forward record)
is exactly the surface that a wire test catches and a unit test misses.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from llm_gateway import app as gateway_app_mod
from llm_gateway.app import _init_app


_FAKE_RESPONSE_BODY: dict[str, Any] = {
    "id": "chatcmpl-fake",
    "object": "chat.completion",
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "ok"},
        "finish_reason": "stop",
    }],
    "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
}


class _FakeResponse:
    """Minimal httpx.Response stand-in. The gateway reads .status_code /
    .content / .headers — that's the surface we need."""

    def __init__(self, status_code: int = 200, body: dict | None = None):
        self.status_code = status_code
        self.content = json.dumps(body if body is not None else _FAKE_RESPONSE_BODY).encode()
        self.headers = {"content-type": "application/json"}


class _FakeHttp:
    """Async upstream mock. Counts POSTs so tests can prove cache hits
    bypassed the network entirely."""

    def __init__(self):
        self.post_count = 0
        self.next_response: _FakeResponse | None = None

    async def post(self, *_a, **_kw):
        self.post_count += 1
        return self.next_response or _FakeResponse()

    async def aclose(self):
        pass


# ── fixtures ────────────────────────────────────────────────────────────────


def _write_config(tmp_path: Path, mode: str, *, replay_with_latency: bool = False,
                  db_path: Path | None = None) -> Path:
    cfg = {
        "backends": [{
            "name": "fake-backend",
            "base_url": "http://upstream.invalid/v1",
            "model": "fake-model",
            "api_key": "EMPTY",
            "request_timeout": 10,
        }],
        "inference_policy": {
            "type": "ordered",
            "order": ["fake-backend"],
        },
    }
    if mode != "disabled":
        cfg["cache"] = {
            "mode": mode,
            "db_path": str(db_path or (tmp_path / "c.db")),
            "replay_with_latency": replay_with_latency,
            "log_every_n": 0,
        }
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp_path / "gw.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    return cfg_path


@pytest.fixture
def boot():
    """Construct a fresh app per test (the gateway uses module-level
    state; sequential isolation only). Yields (client, fake_http) and
    closes the TestClient on teardown."""
    created: list[tuple[TestClient, _FakeHttp]] = []

    def _boot(cfg_path: Path):
        app = _init_app(str(cfg_path))
        client = TestClient(app)
        client.__enter__()   # triggers startup, populates _state
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
        {"role": "system", "content": "You are a bug-fix agent."},
        {"role": "user", "content": "fix this please"},
    ],
    "temperature": 0.0,
}


# ── disabled mode: pure pass-through ────────────────────────────────────────


def test_disabled_mode_does_not_create_db(tmp_path, boot):
    """No cache section ⇒ no sqlite file, no cache headers in the
    response. Confirms the legacy code path is byte-identical when cache
    is off."""
    cfg = _write_config(tmp_path, "disabled")
    client, fake = boot(cfg)
    r = client.post("/v1/chat/completions", json=_BODY)
    assert r.status_code == 200
    assert fake.post_count == 1
    assert "x-sdlcma-cache" not in {k.lower() for k in r.headers}
    assert not (tmp_path / "c.db").exists()


# ── record mode: forward + persist ──────────────────────────────────────────


def test_record_mode_forwards_and_persists(tmp_path, boot):
    """Record mode = always forward, also write to cache. The cached
    body must match the upstream body byte-for-byte (so the next replay
    is faithful), and the response carries the `miss` cache header."""
    cfg = _write_config(tmp_path, "record")
    client, fake = boot(cfg)

    r = client.post("/v1/chat/completions", json=_BODY)
    assert r.status_code == 200
    assert fake.post_count == 1
    assert r.headers.get("x-sdlcma-cache") == "miss"
    assert r.headers.get("x-sdlcma-cache-key")

    # /cache/stats should show one record, zero hits.
    stats = client.get("/cache/stats").json()
    assert stats["enabled"] is True
    assert stats["records"] == 1
    assert stats["hits"] == 0
    assert stats["entry_count"] == 1


# ── cache mode: hit on repeat ───────────────────────────────────────────────


def test_cache_mode_hits_on_second_call(tmp_path, boot):
    """The headline property: in cache mode, the second identical
    request returns the same body WITHOUT consulting upstream. This is
    the foundation of zero-token stress tests."""
    cfg = _write_config(tmp_path, "cache")
    client, fake = boot(cfg)

    # First call: miss, upstream called.
    r1 = client.post("/v1/chat/completions", json=_BODY)
    assert r1.status_code == 200
    assert fake.post_count == 1
    assert r1.headers.get("x-sdlcma-cache") == "miss"

    # Second call: same body → hit, upstream NOT touched again.
    r2 = client.post("/v1/chat/completions", json=_BODY)
    assert r2.status_code == 200
    assert fake.post_count == 1
    assert r2.headers.get("x-sdlcma-cache") == "hit"
    assert r2.json() == r1.json()
    # Backend name preserved across the hit (the gateway returns the
    # backend that originally served the call — useful for telemetry).
    assert r2.headers.get("x-sdlcma-backend-name") == "fake-backend"

    stats = client.get("/cache/stats").json()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["records"] == 1
    assert stats["hit_rate"] == pytest.approx(0.5)


def test_cache_mode_assistant_turn_still_hits(tmp_path, boot):
    """The whole reason we drop assistant messages from the key: a
    second-turn request that carries the LLM's first-turn response must
    still hit the cache. Without this property the ReAct loop misses on
    every turn after the first."""
    cfg = _write_config(tmp_path, "cache")
    client, fake = boot(cfg)

    # Turn 1 (the body that would be the user-only prompt).
    client.post("/v1/chat/completions", json=_BODY)
    assert fake.post_count == 1

    # Turn 1' — same external content, plus an assistant message
    # carrying a randomly-IDed tool call (the LLM's previous output).
    body_with_assistant = json.loads(json.dumps(_BODY))
    body_with_assistant["messages"].append({
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call_RANDOM_ID_12345",
            "type": "function",
            "function": {"name": "fetch_file",
                         "arguments": '{"path": "calc.py"}'},
        }],
    })
    r = client.post("/v1/chat/completions", json=body_with_assistant)
    assert r.status_code == 200
    assert fake.post_count == 1   # still 1 — the cache absorbed it
    assert r.headers.get("x-sdlcma-cache") == "hit"


# ── replay mode: strict cache-only ──────────────────────────────────────────


def test_replay_mode_hit_returns_cached(tmp_path, boot):
    """Pre-populate via record mode, then re-boot in replay mode. The
    same request hits without ever touching upstream (the replay
    machine doesn't even need API keys / network)."""
    db = tmp_path / "shared.db"
    cfg_rec = _write_config(tmp_path / "rec", "record", db_path=db)
    cfg_rec.parent.mkdir(exist_ok=True)
    client_rec, fake_rec = boot(cfg_rec)
    client_rec.post("/v1/chat/completions", json=_BODY)
    assert fake_rec.post_count == 1

    cfg_rep = _write_config(tmp_path / "rep", "replay", db_path=db)
    cfg_rep.parent.mkdir(exist_ok=True)
    client_rep, fake_rep = boot(cfg_rep)
    r = client_rep.post("/v1/chat/completions", json=_BODY)
    assert r.status_code == 200
    assert fake_rep.post_count == 0   # upstream NEVER consulted
    assert r.headers.get("x-sdlcma-cache") == "hit"


def test_replay_mode_miss_returns_409(tmp_path, boot):
    """Strict mode: cache miss is a hard error, distinguishable from
    `502 backend down`. 409 was picked over 404 because the request is
    semantically valid — it's the cache state that's "conflicting" with
    what we expect."""
    cfg = _write_config(tmp_path, "replay", db_path=tmp_path / "empty.db")
    client, fake = boot(cfg)
    r = client.post("/v1/chat/completions", json=_BODY)
    assert r.status_code == 409
    assert fake.post_count == 0
    body = r.json()
    assert body["error"]["type"] == "cache_miss_replay"
    assert r.headers.get("x-sdlcma-cache") == "miss"


# ── replay_with_latency: sleep to preserve phase-3 realism ──────────────────


def test_replay_with_latency_sleeps_to_recorded_wallclock(tmp_path, boot,
                                                          monkeypatch):
    """`replay_with_latency: true` means a hit sleeps to the original
    recorded wallclock before returning, keeping stress-test latency
    numbers honest. We patch asyncio.sleep to assert the requested
    duration — the test is about the call, not the wall time."""
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(gateway_app_mod.asyncio, "sleep", fake_sleep)

    # Pre-populate via record mode with a known wallclock.
    db = tmp_path / "lat.db"
    from llm_gateway.cache import Cache
    from llm_gateway.keying import derive_key
    pre = Cache(db)
    body_bytes = json.dumps(_FAKE_RESPONSE_BODY).encode()
    pre.put(derive_key(_BODY), body_bytes, 200,
            original_wallclock_ms=2345, original_backend_name="fake-backend")
    pre.close()

    cfg = _write_config(tmp_path / "rwl", "replay",
                        db_path=db, replay_with_latency=True)
    cfg.parent.mkdir(exist_ok=True)
    client, _ = boot(cfg)
    r = client.post("/v1/chat/completions", json=_BODY)
    assert r.status_code == 200
    # The handler should have called sleep with 2.345 s (capped to 60).
    assert sleeps == [2.345]


# ── /cache/stats endpoint shape ────────────────────────────────────────────


def test_cache_stats_endpoint_disabled_returns_enabled_false(tmp_path, boot):
    cfg = _write_config(tmp_path, "disabled")
    client, _ = boot(cfg)
    r = client.get("/cache/stats")
    assert r.status_code == 200
    assert r.json() == {"enabled": False}


def test_cache_mode_normalize_content_hits_across_timestamp_drift(tmp_path, boot):
    """End-to-end: with `normalize_content: true` in the YAML, two
    requests carrying GitLab CI traces that differ ONLY in timestamps
    and machinery metadata must hit the cache on the second call. This
    is the exact stress-test scenario that surfaced the 0 % hit rate on
    2026-05-29 — pin it so a regression in keying.py or config wiring
    can't silently re-break it."""
    cfg = _write_config(tmp_path, "cache")
    # Re-write with normalize_content enabled (the helper doesn't take
    # this kwarg yet — append the field manually so we don't bloat the
    # helper for one test).
    raw = json.loads(cfg.read_text())
    raw["cache"]["normalize_content"] = True
    cfg.write_text(json.dumps(raw))

    client, fake = boot(cfg)

    trace1 = (
        "2026-05-28T22:02:12.634090Z 00O Running with gitlab-runner 19.0.0\n"
        "2026-05-28T22:02:13.000000Z 00O section_start:1780005732:prepare_executor\n"
        "2026-05-28T22:02:21.589663Z 01O FAILED test_calc.py::test_add - assert 4 == 5\n"
    )
    trace2 = (
        "2026-05-29T03:14:09.999999Z 00O Running with gitlab-runner 19.0.0\n"
        "2026-05-29T03:14:10.111111Z 00O section_start:1780051999:prepare_executor\n"
        "2026-05-29T03:14:25.222222Z 01O FAILED test_calc.py::test_add - assert 4 == 5\n"
    )

    body = {**_BODY, "messages": [
        {"role": "system", "content": "You are a Python bug fix agent."},
        {"role": "user", "content": f"## CI failure info\n{trace1}"},
    ]}
    # Turn-1: cold cache, must miss + record (upstream called).
    r1 = client.post("/v1/chat/completions", json=body)
    assert r1.status_code == 200
    assert fake.post_count == 1
    assert r1.headers.get("x-sdlcma-cache") == "miss"

    # Turn-2: same fixture, new pipeline → trace differs only in metadata.
    # Must hit; upstream MUST NOT be called.
    body2 = {**body, "messages": [
        {"role": "system", "content": "You are a Python bug fix agent."},
        {"role": "user", "content": f"## CI failure info\n{trace2}"},
    ]}
    r2 = client.post("/v1/chat/completions", json=body2)
    assert r2.status_code == 200
    assert fake.post_count == 1   # NOT 2 — the win condition
    assert r2.headers.get("x-sdlcma-cache") == "hit"


def test_cache_mode_normalize_off_misses_on_timestamp_drift(tmp_path, boot):
    """Pin the default-off behaviour: same scenario without the flag
    re-creates the 0 % hit rate that motivated normalize_content in the
    first place. If a future change defaults the flag on, this test
    surfaces it as an intentional decision."""
    cfg = _write_config(tmp_path, "cache")  # normalize_content defaults False
    client, fake = boot(cfg)

    trace1 = "2026-05-28T22:02:12.634090Z 00O FAILED test_calc.py - X"
    trace2 = "2026-05-29T03:14:09.999999Z 00O FAILED test_calc.py - X"
    body1 = {**_BODY, "messages": [
        {"role": "system", "content": "X"},
        {"role": "user", "content": trace1},
    ]}
    body2 = {**_BODY, "messages": [
        {"role": "system", "content": "X"},
        {"role": "user", "content": trace2},
    ]}
    client.post("/v1/chat/completions", json=body1)
    client.post("/v1/chat/completions", json=body2)
    # Both miss — upstream called twice.
    assert fake.post_count == 2


def test_cache_stats_endpoint_includes_hit_rate(tmp_path, boot):
    cfg = _write_config(tmp_path, "cache")
    client, _ = boot(cfg)
    # 1 miss + 2 hits → hit_rate ≈ 0.667 over the lookup attempts (3 calls;
    # the 1st is a miss-then-record, the 2nd & 3rd are hits).
    client.post("/v1/chat/completions", json=_BODY)
    client.post("/v1/chat/completions", json=_BODY)
    client.post("/v1/chat/completions", json=_BODY)
    stats = client.get("/cache/stats").json()
    assert stats["hits"] == 2
    assert stats["misses"] == 1
    assert stats["records"] == 1
    assert stats["entry_count"] == 1
    assert stats["hit_rate"] == pytest.approx(2 / 3)
    assert stats["db_path"].endswith(".db")
