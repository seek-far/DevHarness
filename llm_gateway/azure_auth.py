"""Microsoft Entra ID bearer tokens for keyless backends.

Deliberately a near-twin of `bf_worker/services/azure_auth.py` rather than a
shared import. **The gateway image does not and must not contain bf_worker** —
each service image installs only its own component (see
`tests/test_image_dependencies.py` and the 2026-05-31 CrashLoop it exists to
prevent). Sharing this file across the two would force one image to bundle the
other's source tree. The duplication is ~40 lines and is the cheaper trade.

The gateway is an asyncio service, so a bare synchronous `get_token()` would
block the event loop for the ~100ms–1s a token mint takes. That happens only
once an hour, but stalling every in-flight request for a second is exactly the
kind of thing that shows up later as an unexplained p99 spike. So the mint runs
in a worker thread via `asyncio.to_thread`.

We deliberately do NOT use `azure.identity.aio`: its async credentials require
**aiohttp** as their transport, and azure-identity does not declare aiohttp as a
dependency — you only discover this when `DefaultAzureCredential` raises
`ImportError: aiohttp package is not installed` at the first request (which is
exactly how this was found, 2026-07-14). Pulling a second HTTP stack into an
httpx-based service, purely for an hourly token refresh, is a bad trade. The
sync credential in a thread gives the same non-blocking property with no extra
dependency.

`azure.identity` is imported lazily so a gateway with no keyless backend does
not need the package installed at all.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Refresh this long before the token actually dies, so a request that is
# selected now cannot be handed a token that expires mid-flight upstream.
_REFRESH_SKEW_S = 300


class GatewayAuthError(RuntimeError):
    """Raised when a bearer token cannot be obtained for a keyless backend."""


@dataclass
class _CachedToken:
    token: str
    expires_on: float


class EntraTokenProvider:
    """Per-(scope, client_id) token cache with async single-flight refresh."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], _CachedToken] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._credentials: dict[str, object] = {}

    def _lock_for(self, key: tuple[str, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _credential(self, client_id: str):
        cred = self._credentials.get(client_id)
        if cred is not None:
            return cred
        try:
            from azure.identity import DefaultAzureCredential
        except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
            raise GatewayAuthError(
                "backend auth=entra needs the `azure-identity` package "
                "(pip install azure-identity)."
            ) from exc
        kwargs = {"managed_identity_client_id": client_id} if client_id else {}
        cred = DefaultAzureCredential(**kwargs)
        self._credentials[client_id] = cred
        return cred

    def _mint(self, scope: str, client_id: str) -> _CachedToken:
        """Blocking. Always called via asyncio.to_thread."""
        cred = self._credential(client_id)
        tok = cred.get_token(scope)
        return _CachedToken(token=tok.token, expires_on=float(tok.expires_on))

    async def get_token(self, scope: str, client_id: str = "") -> str:
        key = (scope, client_id or "")

        cached = self._cache.get(key)
        if cached is not None and cached.expires_on - _REFRESH_SKEW_S > time.time():
            return cached.token

        async with self._lock_for(key):
            # Re-check under the lock: a concurrent request may have refreshed
            # while we waited. Without this, a burst on a cold cache mints one
            # token per in-flight request.
            cached = self._cache.get(key)
            if cached is not None and cached.expires_on - _REFRESH_SKEW_S > time.time():
                return cached.token

            try:
                # Off the event loop: the mint is a blocking HTTPS round-trip.
                fresh = await asyncio.to_thread(self._mint, scope, client_id or "")
            except GatewayAuthError:
                raise
            except Exception as exc:
                raise GatewayAuthError(
                    f"could not obtain an Entra token for scope {scope!r}: {exc}. "
                    "On Azure compute, check the managed identity is assigned and "
                    "holds a data-plane role (e.g. 'Cognitive Services OpenAI User'). "
                    "Locally, run `az login`."
                ) from exc

            self._cache[key] = fresh
            logger.info(
                "gateway: minted Entra token scope=%s client_id=%s ttl=%ds",
                scope, client_id or "(default)", int(fresh.expires_on - time.time()),
            )
            return fresh.token

    async def aclose(self) -> None:
        for cred in self._credentials.values():
            close = getattr(cred, "close", None)
            if close is not None:
                try:
                    close()  # sync credential
                except Exception:  # pragma: no cover - best effort on shutdown
                    pass
        self._credentials.clear()

    def reset(self) -> None:
        """Drop cached tokens. For tests."""
        self._cache.clear()
        self._locks.clear()
