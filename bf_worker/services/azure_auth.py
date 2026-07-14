"""
services/azure_auth.py

Microsoft Entra ID access tokens for keyless LLM backends (Azure OpenAI /
Foundry). Used when ``cfg.llm_auth_mode == "entra"``.

Why this is so small: Azure's ``/openai/v1/`` route authenticates with a plain
``Authorization: Bearer <token>`` header — the same header the OpenAI client
already emits for ``api_key``. So keyless Azure needs no HTTP-layer special
case at all; it only needs a *different source* for the string that goes in
the key slot. That is this module's whole job.

``DefaultAzureCredential`` resolves to a managed identity when running on Azure
compute and to the developer's ``az login`` session locally. Both paths mint a
token for the same scope and are indistinguishable downstream, so the local run
genuinely exercises the Managed Identity code path.

``azure.identity`` is imported lazily so deployments that never use Entra auth
(every self-hosted / Dashscope / vLLM setup) don't need the package installed.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Refresh this many seconds before the token actually expires. A long-running
# fix() must never hand a token to the LLM client that dies mid-flight.
_REFRESH_SKEW_S = 300


class AzureAuthError(RuntimeError):
    """Raised when an Entra token cannot be obtained.

    Deliberately fatal: a worker configured for keyless auth that silently
    fell back to an empty key would fail later with a confusing 401 from the
    model endpoint instead of a clear credential error here.
    """


@dataclass
class _CachedToken:
    token: str
    expires_on: float  # unix seconds


# Cache is keyed by (scope, client_id) so a process talking to two differently-
# scoped backends doesn't cross-contaminate. The lock makes token acquisition
# single-flight: N concurrent callers on a cold cache mint ONE token, not N.
_CACHE: dict[tuple[str, str], _CachedToken] = {}
_LOCK = threading.Lock()


def _fetch_token(scope: str, client_id: str) -> _CachedToken:
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise AzureAuthError(
            "llm_auth_mode='entra' needs the `azure-identity` package "
            "(pip install azure-identity)."
        ) from exc

    kwargs = {"managed_identity_client_id": client_id} if client_id else {}
    credential = DefaultAzureCredential(**kwargs)
    try:
        tok = credential.get_token(scope)
    except Exception as exc:
        raise AzureAuthError(
            f"could not obtain an Entra token for scope {scope!r}: {exc}. "
            "On Azure compute, check the managed identity is assigned and has "
            "a data-plane role (e.g. 'Cognitive Services OpenAI User'). "
            "Locally, run `az login`."
        ) from exc

    return _CachedToken(token=tok.token, expires_on=float(tok.expires_on))


def get_entra_token(scope: str, client_id: str = "") -> str:
    """Return a valid Entra access token for `scope`, minting/refreshing as needed."""
    key = (scope, client_id or "")
    now = time.time()

    cached = _CACHE.get(key)
    if cached is not None and cached.expires_on - _REFRESH_SKEW_S > now:
        return cached.token

    with _LOCK:
        # Re-check under the lock: another thread may have refreshed while we
        # waited, in which case we must not mint a second token.
        cached = _CACHE.get(key)
        if cached is not None and cached.expires_on - _REFRESH_SKEW_S > time.time():
            return cached.token

        fresh = _fetch_token(scope, client_id or "")
        _CACHE[key] = fresh
        logger.info(
            "azure_auth: minted Entra token scope=%s client_id=%s ttl=%ds",
            scope, client_id or "(default)",
            int(fresh.expires_on - time.time()),
        )
        return fresh.token


def reset_cache() -> None:
    """Drop all cached tokens. For tests."""
    with _LOCK:
        _CACHE.clear()
