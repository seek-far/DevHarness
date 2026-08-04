"""Google Cloud bearer tokens for keyless Vertex AI backends.

The GCP twin of `llm_gateway/azure_auth.py`, and deliberately shaped like it:
same cache-with-skew, same async single-flight, same lazy import, same "the
mint is a blocking HTTPS round-trip so it runs in a worker thread" rule. Read
that file's header for the reasoning — all of it applies here unchanged.

Why this needs no Vertex-specific HTTP path at all: Vertex AI exposes an
OpenAI-compatible route at

    https://<loc>-aiplatform.googleapis.com/v1/projects/<proj>/locations/<loc>/endpoints/openapi

which authenticates with an ordinary `Authorization: Bearer <token>`. That is
the same header a static API key produces, which is why `auth: gcp` only
changes where the bearer comes from — exactly the property that made the Azure
backend cheap.

Credentials come from **Application Default Credentials**, so the resolution
order is Google's, not ours:

  * on GCP compute (Cloud Run, GCE, GKE) → the attached service account
  * locally → whatever `gcloud auth application-default login` wrote

That is the same "local run genuinely exercises the cloud code path" property
DefaultAzureCredential gives on the Azure side. Note it is specifically the
ADC login that matters: a plain `gcloud auth login` authenticates the CLI and
leaves ADC unset, which surfaces here as DefaultCredentialsError.

`google.auth` is imported lazily so a gateway with no Vertex backend does not
need the package installed at all.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timezone

logger = logging.getLogger(__name__)

# Mirror of azure_auth._REFRESH_SKEW_S, same rationale: never hand a request a
# token that can expire while it is still in flight upstream.
_REFRESH_SKEW_S = 300

# Vertex AI accepts the broad cloud-platform scope. ADC user credentials from
# `gcloud auth application-default login` are minted with this scope anyway.
DEFAULT_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class GcpAuthError(RuntimeError):
    """Raised when a bearer token cannot be obtained for a Vertex backend."""


@dataclass
class _CachedToken:
    token: str
    expires_on: float  # epoch seconds


def _expiry_to_epoch(expiry) -> float:
    """google.auth's `credentials.expiry` → epoch seconds.

    ⚠️ The load-bearing line in this file. `expiry` is a **naive** datetime
    that google.auth documents as UTC. Calling `.timestamp()` on a naive
    datetime makes Python interpret it in the machine's LOCAL timezone, so the
    epoch comes out wrong by the UTC offset — and the sign of that error
    decides whether the bug is cosmetic or a production outage:

      * UTC+N host (this dev box is in Germany, UTC+1/+2): the token looks
        like it expired earlier than it did → we refresh on every request.
        Wasteful, self-healing, easy to miss forever.
      * UTC-N host (any us-* deployment): the token looks like it expires
        LATER than it does → we keep sending an expired token and Vertex
        answers 401 for a whole UTC-offset's worth of requests.

    So we stamp UTC explicitly rather than trusting the ambient timezone.
    A missing expiry means "unknown" — treated as already-expired by the
    caller, which costs a refresh but can never serve a stale token.
    """
    if expiry is None:
        return 0.0
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry.timestamp()


class GcpTokenProvider:
    """Per-scope token cache with async single-flight refresh."""

    def __init__(self) -> None:
        self._cache: dict[str, _CachedToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._credentials: dict[str, object] = {}

    def _lock_for(self, scope: str) -> asyncio.Lock:
        lock = self._locks.get(scope)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[scope] = lock
        return lock

    def _credential(self, scope: str):
        cred = self._credentials.get(scope)
        if cred is not None:
            return cred
        try:
            import google.auth
        except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
            raise GcpAuthError(
                "backend auth=gcp needs the `google-auth` package "
                "(pip install google-auth)."
            ) from exc
        # google.auth.default() also returns the resolved project id; we do not
        # use it. The project is already baked into the backend's base_url, and
        # letting ADC decide it there would mean the URL and the credential
        # could silently disagree about which project is being billed.
        cred, _project = google.auth.default(scopes=[scope])
        self._credentials[scope] = cred
        return cred

    def _mint(self, scope: str) -> _CachedToken:
        """Blocking. Always called via asyncio.to_thread."""
        import google.auth.transport.requests

        cred = self._credential(scope)
        cred.refresh(google.auth.transport.requests.Request())
        return _CachedToken(
            token=cred.token,
            expires_on=_expiry_to_epoch(getattr(cred, "expiry", None)),
        )

    async def get_token(self, scope: str = DEFAULT_SCOPE) -> str:
        scope = scope or DEFAULT_SCOPE

        cached = self._cache.get(scope)
        if cached is not None and cached.expires_on - _REFRESH_SKEW_S > time.time():
            return cached.token

        async with self._lock_for(scope):
            # Re-check under the lock: a concurrent request may have refreshed
            # while we waited. Without this, a burst on a cold cache mints one
            # token per in-flight request.
            cached = self._cache.get(scope)
            if cached is not None and cached.expires_on - _REFRESH_SKEW_S > time.time():
                return cached.token

            try:
                # Off the event loop: the mint is a blocking HTTPS round-trip.
                fresh = await asyncio.to_thread(self._mint, scope)
            except GcpAuthError:
                raise
            except Exception as exc:
                raise GcpAuthError(
                    f"could not obtain a Google Cloud token for scope {scope!r}: {exc}. "
                    "On GCP compute, check the attached service account holds "
                    "`roles/aiplatform.user`. Locally, run "
                    "`gcloud auth application-default login` — note that a plain "
                    "`gcloud auth login` authenticates the CLI only and does NOT "
                    "set up ADC."
                ) from exc

            if not fresh.token:
                raise GcpAuthError(
                    f"google.auth returned an empty token for scope {scope!r}"
                )

            self._cache[scope] = fresh
            logger.info(
                "gateway: minted GCP token scope=%s ttl=%ds",
                scope, int(fresh.expires_on - time.time()),
            )
            return fresh.token

    async def aclose(self) -> None:
        # google.auth credentials hold no socket of their own (each refresh
        # builds its own transport), so there is nothing to close. Kept for
        # symmetry with EntraTokenProvider so app shutdown treats both alike.
        self._credentials.clear()

    def reset(self) -> None:
        """Drop cached tokens. For tests."""
        self._cache.clear()
        self._locks.clear()
