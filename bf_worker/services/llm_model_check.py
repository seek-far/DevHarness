"""
services/llm_model_check.py

Startup sanity check: when pointed at a self-hosted backend, verify that the
model name in worker settings (cfg.llm_model) matches what the backend is
actually serving on ``GET {LLM_API_BASE_URL}/models``.

Why this exists
---------------
Self-hosted setups (vLLM, llama.cpp, Ollama) decouple two things that must
agree but often drift:

  - what the backend exposes (the ``--served-model-name`` flag, or its default)
  - what the env file declares (``LLM_MODEL=...``)

When these disagree, OpenAI-compatible servers don't all behave the same way:
some 404, some silently serve a different model, some happily accept the
wrong name and use whatever they have loaded. The silent path is the
dangerous one — an eval sweep "ran successfully" against the wrong model
without anyone noticing, contaminating fix-rate / latency comparisons.

Cloud backends (Dashscope, OpenAI) are NOT checked here — they validate
model names server-side and reject with a clear 4xx if you typo. The
discriminator for "self-hosted" is ``cfg.llm_api_key == "EMPTY"``, the
project's existing convention (settings/worker_settings.py:llm_api_key
default; see also CLAUDE.md Configuration section).

Policy (per project decision 2026-05-24)
----------------------------------------
- Cloud backend  → no probe, return None.
- Self-hosted, network fails → log a warning, return None. Don't block the
  run: the backend may simply be slow to come up, and the next real LLM call
  will fail with full context if it's genuinely down.
- Self-hosted, names match → return the served name (caller stashes it on
  RunRecord.llm_model_served).
- Self-hosted, names mismatch → raise SystemExit unless the env var
  ``LLM_ALLOW_MODEL_MISMATCH=1`` is set (for one-offs / known intentional
  divergence). The error message prints both names so the operator knows
  what to fix.

The probe runs once per entry-point at startup. It is NOT called per LLM
request — that would add a network round trip to every call.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# Connect+read timeout for the /models probe. Sized for "backend is up but
# warming a model" — long enough that a paging-in vLLM doesn't false-fail,
# short enough that a wrong-port misconfiguration trips fast.
_PROBE_TIMEOUT_S = 10.0

# Env var to skip the abort on mismatch. Set deliberately for one-off runs
# where you know the names diverge intentionally. Same name across all 3
# entry points (worker, standalone, eval runner).
_ALLOW_ENV = "LLM_ALLOW_MODEL_MISMATCH"


def is_self_hosted(cfg: Any) -> bool:
    """Project convention: ``LLM_API_KEY=="EMPTY"`` means self-hosted.

    See settings/worker_settings.py:llm_api_key default and the CLAUDE.md
    Configuration section. Cloud backends override the key via the env file.
    """
    return getattr(cfg, "llm_api_key", None) == "EMPTY"


def query_served_model(base_url: str, timeout_s: float = _PROBE_TIMEOUT_S) -> str:
    """GET ``{base_url}/models``, return the first ``data[].id``.

    Raises any URLError / HTTPError / JSON / KeyError straight up — the caller
    decides whether to warn-and-continue or abort.
    """
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
        body = resp.read()
    payload = json.loads(body)
    data = payload.get("data") or []
    if not data:
        raise ValueError(f"{url} returned no models in `data`: {payload!r}")
    served = data[0].get("id")
    if not served:
        raise ValueError(f"{url} returned a model without an id: {data[0]!r}")
    return str(served)


def check_or_abort(cfg: Any) -> str | None:
    """Run the startup check; return served name (or None when not applicable).

    Behaviour by case:

      llm_via_gateway = True                         → return None (skip probe)
      cloud backend  (cfg.llm_api_key != "EMPTY")    → return None
      self-hosted, network fails                     → log + return None
      self-hosted, env model == served               → return served
      self-hosted, mismatch + override env set       → log + return served
      self-hosted, mismatch + no override            → raise SystemExit
    """
    # Gateway mode: the worker's LLM endpoint is an SDLCMA llm_gateway, which
    # exposes the UNION of configured backend models on /v1/models. There is
    # no single "served" name to check against cfg.llm_model — the chosen
    # backend (and thus the served model) is selected per-request by the
    # gateway's inference policy. The gateway itself enforces backend model
    # discipline via its config; the worker-side probe would be a category
    # error here.
    if getattr(cfg, "llm_via_gateway", False):
        logger.info(
            "llm_model_check: skipped (llm_via_gateway=True; gateway routes per request)"
        )
        return None
    if not is_self_hosted(cfg):
        return None

    base_url = getattr(cfg, "llm_api_base_url", "") or ""
    declared = getattr(cfg, "llm_model", "") or ""
    if not base_url:
        logger.warning(
            "llm_model_check: self-hosted backend but LLM_API_BASE_URL is empty; "
            "skipping probe"
        )
        return None

    try:
        served = query_served_model(base_url)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            json.JSONDecodeError, OSError) as exc:
        # Backend may simply be slow to come up; the next real LLM call will
        # fail with full context if it's genuinely down. Don't block on the
        # probe.
        logger.warning(
            "llm_model_check: probe %s/models failed (%s: %s) — skipping check, "
            "run will proceed", base_url.rstrip("/"), type(exc).__name__, exc,
        )
        return None

    if declared == served:
        logger.info(
            "llm_model_check: env LLM_MODEL matches backend (%s)", served
        )
        return served

    if os.environ.get(_ALLOW_ENV) == "1":
        logger.warning(
            "llm_model_check: MISMATCH bypassed by %s=1 — env=%r, served=%r",
            _ALLOW_ENV, declared, served,
        )
        return served

    msg = (
        f"\n\nLLM model mismatch (self-hosted backend at {base_url}):\n"
        f"  env LLM_MODEL = {declared!r}\n"
        f"  served by /v1/models = {served!r}\n"
        f"\n"
        f"Aligning these is mandatory: an OpenAI-compatible backend may "
        f"silently serve the loaded model regardless of the requested name, "
        f"contaminating eval and bug-fix runs without an error.\n"
        f"\n"
        f"Fix one of:\n"
        f"  - set LLM_MODEL={served!r} in the worker env file, or\n"
        f"  - restart the backend with --served-model-name={declared!r}, or\n"
        f"  - set {_ALLOW_ENV}=1 for THIS run only (use sparingly)."
    )
    raise SystemExit(msg)
