"""Gateway Prometheus metrics.

Three metrics, low-cardinality labels only — production-safe at any
project count:

  sdlcma_webhooks_received_total{object_kind, classification}
      Counts every POST /webhook. `object_kind` is GitLab's payload type
      (`pipeline`, `push`, …); `classification` is what the orchestrator's
      parser would call it (`bug_reported`, `validation`, `other`, `invalid`).
      Lets a dashboard split "real bug reports" from "ignored noise" so
      throughput math reflects actual workload.

  sdlcma_webhooks_forwarded_total
      Counts successful XADDs to gateway:stream. Delta vs `received` =
      Redis-layer failures, a hard alerting signal.

  sdlcma_gateway_handle_ms (histogram)
      Wallclock from request receive to XADD completion. Phase-1
      latency view, distinct from the per-bug `phase=gateway_received`
      log line (which doesn't capture the XADD wait).

Module-level globals are intentional — prometheus_client uses a process-
wide default registry, and the gateway process is single-process by
default. Tests use BEFORE/AFTER deltas so the leaky-global doesn't bite.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram


WEBHOOKS_RECEIVED = Counter(
    "sdlcma_webhooks_received_total",
    "Total /webhook POSTs by GitLab object_kind and parser classification",
    ["object_kind", "classification"],
)

WEBHOOKS_FORWARDED = Counter(
    "sdlcma_webhooks_forwarded_total",
    "Total webhooks successfully XADDed to gateway:stream",
)

WEBHOOK_HANDLE_MS = Histogram(
    "sdlcma_gateway_handle_ms",
    "Gateway request wallclock — receive → XADD complete (ms)",
    # Tuned for the observed phase-1 distribution (p50 ~1ms, p95 ~2ms
    # under burst): tight low buckets, wide tail so a 1s spike is visible
    # without dominating the rendering.
    buckets=(1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 5000),
)


def classify_webhook(payload: dict) -> str:
    """Return the classification a downstream parser would produce.

    Mirrors orchestrator.parser.parse_message's decision tree without
    importing it — the metric needs to be cheap and self-contained.
    Validation refs (auto/*) are split from genuine bug_reported so
    dashboards can see "GitLab fired N webhooks because of OUR pushes"
    distinct from "GitLab fired N because of REAL failures".
    """
    if not isinstance(payload, dict) or "object_attributes" not in payload:
        return "invalid"
    ref = (payload["object_attributes"] or {}).get("ref", "")
    status = (payload["object_attributes"] or {}).get("status", "")
    if isinstance(ref, str) and ref.startswith("auto/"):
        return "validation"
    if payload.get("object_kind") == "pipeline" and status == "failed":
        return "bug_reported"
    return "other"
