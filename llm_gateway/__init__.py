"""LLM Gateway service — OpenAI-compatible passthrough with multi-backend
inference policy.

This package is intentionally independent of `bf_worker/`. The worker only
sees the gateway as an OpenAI-compatible endpoint via `LLM_API_BASE_URL`;
the gateway uses the `X-Sdlcma-Bug-Id` and `X-Sdlcma-Attempt` headers to
route each request to a backend per the configured inference policy.

Opt-in: when no gateway is deployed, workers point `LLM_API_BASE_URL`
directly at the upstream backend as before. There is no client-side dep.
"""
