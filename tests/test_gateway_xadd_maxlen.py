"""Gateway XADD MAXLEN guardrail (Item 6.1).

The gateway must pass `maxlen=N, approximate=True` on every XADD so the
stream cannot grow unboundedly across uptime. Without this guard, the
Redis-Streams "ACK does NOT delete entries" semantic means XLEN climbs
forever (verified live in stress_test_1: 19 bugs → 313 entries).

These tests pin:
  * Every `/webhook` POST results in xadd with `maxlen=<cap>,
    approximate=True`. The cap value is read from GatewaySettings.
  * The phase_marker log line is still emitted regardless of trim path.
  * The hot path is otherwise byte-identical (status 200, body unchanged).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway import gateway as gw_mod  # noqa: E402


@pytest.fixture
def client_with_fake_redis():
    """Inject a stub Redis + stub config so xadd is observable without
    touching a real Redis. `override()` is the test-only seam already
    present on the gateway."""
    fake_redis = MagicMock()
    cfg = SimpleNamespace(
        use_redis=True,
        gateway_stream="gateway:stream",
        gateway_stream_maxlen=1234,    # test sentinel — easy to grep
    )
    gw_mod.override(cfg, fake_redis)
    yield TestClient(gw_mod.app), fake_redis, cfg
    # Reset module state so subsequent tests don't see this fake.
    gw_mod.override(None, None)


def _pipeline_payload() -> dict:
    return {
        "object_kind": "pipeline",
        "object_attributes": {"ref": "main", "status": "failed"},
        "project": {"id": 1, "web_url": "http://gitlab.local/x/y"},
        "builds": [{"id": 42}],
    }


def test_xadd_called_with_maxlen_and_approximate(client_with_fake_redis):
    """Headline contract: maxlen + approximate=True are present on every
    XADD. Without this the gateway:stream grows unboundedly."""
    client, fake, cfg = client_with_fake_redis
    r = client.post("/webhook", json=_pipeline_payload())
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}

    fake.xadd.assert_called_once()
    args, kwargs = fake.xadd.call_args
    # Stream name positional
    assert args[0] == "gateway:stream"
    # MAXLEN sentinel from the test config
    assert kwargs.get("maxlen") == 1234
    # Approximate must be True — exact MAXLEN is O(log N) per write and
    # noticeably slower under burst. The default-False of xadd would
    # silently degrade us, so pin it.
    assert kwargs.get("approximate") is True


def test_xadd_called_for_every_webhook(client_with_fake_redis):
    """Three webhooks → three trim-bounded XADDs. Sanity: the cap is
    per-call, not a one-shot setup."""
    client, fake, _ = client_with_fake_redis
    for _ in range(3):
        client.post("/webhook", json=_pipeline_payload())
    assert fake.xadd.call_count == 3
    for call in fake.xadd.call_args_list:
        assert call.kwargs.get("approximate") is True
        assert isinstance(call.kwargs.get("maxlen"), int)


def test_phase_marker_emitted_alongside_trim(client_with_fake_redis, caplog):
    """The phase_marker log line is the join key for the four-phase view;
    must not be lost just because we now pass extra kwargs to xadd."""
    import logging
    caplog.set_level(logging.INFO, logger="gateway.gateway")
    client, _, _ = client_with_fake_redis
    client.post("/webhook", json=_pipeline_payload())
    assert any("phase_marker phase=gateway_received" in r.message
               for r in caplog.records)


def test_default_maxlen_is_set_in_settings():
    """Pin the default 10000 in GatewaySettings — if someone bumps the
    default down to something tiny by accident, this test trips. The
    rationale (500x headroom over largest observed burst of 38) is in
    the field docstring."""
    from gateway.gateway_settings import GatewaySettings
    # Build a fresh instance with no env file overrides; the bare default
    # is what matters.
    assert GatewaySettings.model_fields["gateway_stream_maxlen"].default == 10000
