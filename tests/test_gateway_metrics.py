"""Gateway Prometheus metrics — counter wiring + /metrics endpoint.

Pins:
  * Each POST /webhook bumps received with correct classification.
  * forwarded counter only bumps when Redis is configured + write succeeds.
  * gateway_handle_ms histogram receives observations.
  * /metrics responds 200 with parseable Prometheus text format.
  * classify_webhook covers the four cases the parser would produce
    (bug_reported, validation, other, invalid).

Tests use BEFORE/AFTER deltas on the process-wide default registry
because prometheus_client metrics are module-level globals — clean
isolation per test would require rebuilding the registry, which is
more disruptive than just reading the delta.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway import gateway as gw_mod  # noqa: E402
from gateway import metrics as gw_metrics  # noqa: E402


def _counter_value(name: str, labels: dict | None = None) -> float:
    """Read a counter's current sample value by name + labels. None or
    empty labels mean the unlabeled counter."""
    sample = REGISTRY.get_sample_value(name, labels or {})
    return sample if sample is not None else 0.0


@pytest.fixture
def client_with_fake_redis():
    fake_redis = MagicMock()
    cfg = SimpleNamespace(
        use_redis=True,
        gateway_stream="gateway:stream",
        gateway_stream_maxlen=1234,
    )
    gw_mod.override(cfg, fake_redis)
    yield TestClient(gw_mod.app), fake_redis
    gw_mod.override(None, None)


def _bug_reported_payload() -> dict:
    return {
        "object_kind": "pipeline",
        "object_attributes": {"ref": "main", "status": "failed"},
        "project": {"id": 1, "web_url": "http://gitlab.local/x/y"},
        "builds": [{"id": 42}],
    }


def _validation_payload() -> dict:
    return {
        "object_kind": "pipeline",
        "object_attributes": {
            "ref": "auto/bf/2026_05_29-12_30_45_3_a4f2-da6734c5",
            "status": "success",
        },
        "project": {"id": 1, "web_url": "http://gitlab.local/x/y"},
        "builds": [{"id": 43}],
    }


# ── classify_webhook ────────────────────────────────────────────────────────


def test_classify_bug_reported():
    assert gw_metrics.classify_webhook(_bug_reported_payload()) == "bug_reported"


def test_classify_validation_auto_ref():
    assert gw_metrics.classify_webhook(_validation_payload()) == "validation"


def test_classify_pipeline_success_is_other():
    """Non-failed pipeline on a non-auto ref isn't a bug report —
    classify as `other` so the dashboard can size noise volume."""
    p = _bug_reported_payload()
    p["object_attributes"]["status"] = "success"
    assert gw_metrics.classify_webhook(p) == "other"


def test_classify_missing_object_attributes_is_invalid():
    assert gw_metrics.classify_webhook({"object_kind": "x"}) == "invalid"


def test_classify_non_dict_payload_is_invalid():
    assert gw_metrics.classify_webhook("not a dict") == "invalid"  # type: ignore[arg-type]


# ── counter / histogram wiring ──────────────────────────────────────────────


def test_received_counter_increments_with_classification(client_with_fake_redis):
    client, _ = client_with_fake_redis
    before = _counter_value(
        "sdlcma_webhooks_received_total",
        {"object_kind": "pipeline", "classification": "bug_reported"},
    )
    client.post("/webhook", json=_bug_reported_payload())
    after = _counter_value(
        "sdlcma_webhooks_received_total",
        {"object_kind": "pipeline", "classification": "bug_reported"},
    )
    assert after == before + 1


def test_received_counter_splits_validation_from_bug_reported(client_with_fake_redis):
    """Two webhooks, same object_kind, different classification → two
    distinct label-set counters bump independently. Without this the
    dashboard can't tell real workload from auto-fix noise."""
    client, _ = client_with_fake_redis
    bug_before = _counter_value(
        "sdlcma_webhooks_received_total",
        {"object_kind": "pipeline", "classification": "bug_reported"},
    )
    val_before = _counter_value(
        "sdlcma_webhooks_received_total",
        {"object_kind": "pipeline", "classification": "validation"},
    )
    client.post("/webhook", json=_bug_reported_payload())
    client.post("/webhook", json=_validation_payload())
    assert _counter_value(
        "sdlcma_webhooks_received_total",
        {"object_kind": "pipeline", "classification": "bug_reported"},
    ) == bug_before + 1
    assert _counter_value(
        "sdlcma_webhooks_received_total",
        {"object_kind": "pipeline", "classification": "validation"},
    ) == val_before + 1


def test_forwarded_counter_increments_on_successful_xadd(client_with_fake_redis):
    client, _ = client_with_fake_redis
    before = _counter_value("sdlcma_webhooks_forwarded_total")
    client.post("/webhook", json=_bug_reported_payload())
    after = _counter_value("sdlcma_webhooks_forwarded_total")
    assert after == before + 1


def test_handle_ms_histogram_observes(client_with_fake_redis):
    """The histogram's _count sample must increment per request.
    Histograms expose `<name>_count` as a regular sample."""
    client, _ = client_with_fake_redis
    before = _counter_value("sdlcma_gateway_handle_ms_count")
    client.post("/webhook", json=_bug_reported_payload())
    after = _counter_value("sdlcma_gateway_handle_ms_count")
    assert after == before + 1


# ── /metrics endpoint ──────────────────────────────────────────────────────


def test_metrics_endpoint_serves_prometheus_text(client_with_fake_redis):
    """The /metrics route must return 200 and Prometheus exposition
    format text containing at least one of our metric names. Any
    non-200 means a scraper would silently fail."""
    client, _ = client_with_fake_redis
    # Drive at least one request so counters exist with values.
    client.post("/webhook", json=_bug_reported_payload())

    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    assert "sdlcma_webhooks_received_total" in body
    assert "sdlcma_webhooks_forwarded_total" in body
    assert "sdlcma_gateway_handle_ms" in body
    # Sanity: format is line-based `key value` after `# HELP` / `# TYPE`.
    assert any(line.startswith("# HELP sdlcma_webhooks_received")
               for line in body.splitlines())


def test_metrics_endpoint_does_not_redirect(client_with_fake_redis):
    """`/metrics` without a trailing slash must respond 200 directly,
    NOT 307 to `/metrics/` like the historical mount-based approach
    did. Prometheus scrapers follow redirects so functional but it
    doubles the scrape cost, and `curl -s` without -L shows an empty
    body which is the exact failure mode that surfaced live
    2026-05-29. Pin the no-redirect contract."""
    client, _ = client_with_fake_redis
    r = client.get("/metrics", follow_redirects=False)
    assert r.status_code == 200
    assert "text/plain" in r.headers.get("content-type", "")


def test_metrics_endpoint_returns_correct_content_type(client_with_fake_redis):
    """The content-type must include the Prometheus exposition format
    version marker (`version=0.0.4`). Without it some scrapers fall
    back to OpenMetrics parsing which fails on certain valid lines."""
    client, _ = client_with_fake_redis
    r = client.get("/metrics")
    ct = r.headers.get("content-type", "")
    assert "version=" in ct, f"content-type missing format version: {ct!r}"
