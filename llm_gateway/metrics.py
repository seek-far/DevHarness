"""LLM Gateway Prometheus metrics.

Two metrics, both production-safe at any project count:

  sdlcma_llm_cache_lookups_total{result, mode}
      result ∈ {hit, miss, disabled}
      mode   ∈ {disabled, record, replay, cache}
      Increments once per chat-completions request. `hit` means the
      cached response was served (no upstream call); `miss` means the
      cache was consulted and missed (request was forwarded upstream,
      and on success the response was written back in cache/record
      modes); `disabled` means the cache wasn't consulted at all.
      Hit rate = rate(result="hit") / rate(total). Tokens saved is
      proportional to hit rate.

  sdlcma_llm_upstream_wallclock_ms{backend}
      Histogram of the time the gateway spent waiting on the chosen
      backend, measured from forward start to httpx return.
      `backend` is the policy-selected backend name (low cardinality:
      typically 1-3 configured backends). Cache hits are NOT observed
      here — they bypass the upstream path entirely.

Cardinality is bounded by config: cache.mode is one of 4 values,
backends are declared in the YAML (1-3 in typical configs). No
per-bug or per-model leaks.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram


CACHE_LOOKUPS = Counter(
    "sdlcma_llm_cache_lookups_total",
    "LLM gateway cache lookups by result and configured mode",
    ["result", "mode"],
)

UPSTREAM_WALLCLOCK_MS = Histogram(
    "sdlcma_llm_upstream_wallclock_ms",
    "LLM gateway time spent on upstream call (ms); cache hits excluded",
    ["backend"],
    # Tuned for LLM call distribution: most calls 1-10s, p99 30-60s,
    # tail bucket catches timeouts. Bucketing too narrow would blow
    # up storage; too wide would lose useful resolution near p50.
    buckets=(100, 500, 1000, 2500, 5000, 10000, 30000, 60000, 120000),
)
