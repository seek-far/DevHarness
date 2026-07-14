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

# ── spend ────────────────────────────────────────────────────────────────────
#
#   sdlcma_llm_upstream_cost_usd_total{backend, model}
#       Money actually spent upstream. Cache hits are NOT counted here — they
#       cost nothing, and folding them in would make the cache look expensive.
#
#   sdlcma_llm_cache_saved_usd_total{backend, model}
#       What a cache hit WOULD have cost, priced with the backend that
#       originally served it. This is the number that justifies the cache, and
#       for a replayed stress test it is the whole bill you didn't pay.
#
#   sdlcma_llm_upstream_tokens_total{backend, model, kind}
#       kind ∈ {input, output, cached_input}. `cached_input` is a SUBSET of
#       `input`, not an addition to it — don't sum the two.
#
# ⚠️ The `upstream_` prefix is load-bearing, not decoration. `tools/
# runrecord_to_metrics.py` already exports `sdlcma_llm_tokens_total{type,
# model_slug}` — journal-derived, recomputed per-RUN on every scrape. These
# here are live, per-CALL, from the gateway. Same name would put two
# semantically different things in one metric: the dashboard's
# `sum by (type)(sdlcma_llm_tokens_total)` would silently absorb gateway series
# that have no `type` label, and summing across both double-counts every token.
# Keep the two families namespaced apart. (Caught 2026-07-14 before it shipped.)
#
# Cardinality: backend names come from the YAML (1-3 typical), models likewise,
# kind is 3 values. No per-bug / per-project labels, ever (project rule).

COST_USD = Counter(
    "sdlcma_llm_upstream_cost_usd_total",
    "Upstream LLM spend in USD, by backend and model (cache hits excluded)",
    ["backend", "model"],
)

COST_SAVED_USD = Counter(
    "sdlcma_llm_cache_saved_usd_total",
    "USD not spent because the response came from the replay cache",
    ["backend", "model"],
)

TOKENS = Counter(
    "sdlcma_llm_upstream_tokens_total",
    "Live per-call LLM tokens at the gateway, by backend/model/kind. Distinct "
    "from the journal-derived sdlcma_llm_tokens_total (per-run, recomputed). "
    "cached_input is a SUBSET of input.",
    ["backend", "model", "kind"],
)
