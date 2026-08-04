"""Google Cloud bearer tokens for keyless Vertex AI backends.

The GCP twin of `llm_gateway/azure_auth.py`: same lazy import, same async
single-flight, same "the mint is a blocking HTTPS round-trip so it runs in a
worker thread" rule. Read that file's header for the reasoning.

Why this needs no Vertex-specific HTTP path at all: Vertex AI exposes an
OpenAI-compatible route at

    https://<loc>-aiplatform.googleapis.com/v1/projects/<proj>/locations/<loc>/endpoints/openapi

which authenticates with an ordinary `Authorization: Bearer <token>` — the same
header a static API key produces. So `auth: gcp` only changes where the bearer
comes from, and the gateway keeps ONE request path for every backend. That is
also why we do not use the `google-genai` / `vertexai` SDKs: they speak Google's
request/response shape, and adopting one would force request translation,
response translation, and per-backend special cases through policy/cost/cache.
We want exactly one thing from Google's stack — a token string.

Credentials come from **Application Default Credentials**, so resolution order
is Google's, not ours:

  * on GCP compute (Cloud Run, GCE, GKE) → the attached service account
  * locally → whatever `gcloud auth application-default login` wrote

Note it is specifically the ADC login that matters: a plain `gcloud auth login`
authenticates the CLI and leaves ADC unset, which surfaces here as
DefaultCredentialsError. The wrapped error message says so, because that is the
single most likely operator mistake.

⚠️ TOKEN LIFECYCLE IS THE LIBRARY'S JOB, NOT OURS — learned the hard way.

The first version of this file cached tokens itself: it converted
`credentials.expiry` to epoch seconds and compared against `time.time()` with a
300s skew. `credentials.expiry` is a NAIVE datetime documented as UTC, and
`datetime.timestamp()` reads a naive value in the machine's LOCAL timezone — so
that conversion was wrong by the UTC offset, harmlessly (over-refreshing) on a
UTC+N box and dangerously (serving expired tokens) on any us-* host.

The bug was self-inflicted. `google.auth` already answers the question
correctly and without the conversion:

    Credentials.expired → utcnow() >= (expiry - REFRESH_THRESHOLD)   # 3m45s
    Credentials.valid   → token is not None and not expired

Both sides of that comparison are naive UTC, so there is no timezone to get
wrong. Deleting our own arithmetic deleted the entire bug class.

What this class still adds on top:

  * **Async.** `refresh()` is a blocking HTTPS round-trip; on an asyncio
    gateway it has to go through a thread or it stalls the event loop.
  * **A diagnosable error.** `DefaultCredentialsError` does not mention that
    `gcloud auth login` is not the command you needed.
  * **Single-flight refresh** — for a narrower reason than "the library has
    none". Locking DOES exist upstream, just not on the path a bare
    `Credentials` object takes:

        google/auth/credentials.py                         no lock
        transport/requests.py        (sync  AuthorizedSession)   no lock
        transport/_aiohttp_requests.py (async AuthorizedSession) asyncio.Lock
        _refresh_worker.RefreshThreadManager                threading.Lock

    `google-cloud-bigquery` runs on the LOCK-FREE sync session, which settles
    the question: concurrent refresh is not a correctness problem. Racing
    refreshes each return a valid token, the last write wins, and the only cost
    is redundant round-trips. We lock because Google chose to on the *async*
    transport — a thread pool bounds a sync client to tens of simultaneous
    refreshes, while one event loop can meet the expiry instant with thousands.
    So this is a peak-shaver, not a safety device, and the fast path below
    never touches it. Two deferred simplifications in docs/gcp.md §2.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# Vertex AI accepts the broad cloud-platform scope. ADC user credentials from
# `gcloud auth application-default login` are minted with this scope anyway.
DEFAULT_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class GcpAuthError(RuntimeError):
    """Raised when a bearer token cannot be obtained for a Vertex backend."""


class GcpTokenProvider:
    """Per-scope credentials with async single-flight refresh.

    There is no token cache here on purpose: a google.auth Credentials object
    already holds its token and knows when it went stale, so a second copy in
    this class would be one more thing that can disagree with reality.
    """

    def __init__(self) -> None:
        self._credentials: dict[str, object] = {}
        self._locks: dict[str, asyncio.Lock] = {}

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

    def _mint(self, scope: str) -> str:
        """Blocking. Always called via asyncio.to_thread.

        `cred.valid` carries google.auth's own 3m45s refresh threshold, so the
        re-check here is not redundant with get_token's: by the time a queued
        caller acquires the lock, the holder may already have refreshed this
        very object.
        """
        import google.auth.transport.requests

        cred = self._credential(scope)
        if not cred.valid:
            cred.refresh(google.auth.transport.requests.Request())
        return cred.token

    async def get_token(self, scope: str = DEFAULT_SCOPE) -> str:
        scope = scope or DEFAULT_SCOPE

        # Fast path: a live token, no lock, no thread hop. This is the case for
        # all but roughly one request an hour.
        cred = self._credentials.get(scope)
        if cred is not None and getattr(cred, "valid", False):
            return cred.token

        async with self._lock_for(scope):
            # Re-check under the lock: a concurrent caller may have refreshed
            # while we waited. Without this a burst on an expired token mints
            # one token per in-flight request — wasteful, not incorrect (see
            # the module docstring; google-cloud-bigquery runs without any such
            # lock on purpose).
            cred = self._credentials.get(scope)
            if cred is not None and getattr(cred, "valid", False):
                return cred.token

            try:
                # Off the event loop: the mint is a blocking HTTPS round-trip.
                token = await asyncio.to_thread(self._mint, scope)
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

            if not token:
                raise GcpAuthError(
                    f"google.auth returned an empty token for scope {scope!r}"
                )

            logger.info("gateway: minted GCP token scope=%s", scope)
            return token

    async def aclose(self) -> None:
        # google.auth credentials hold no socket of their own (each refresh
        # builds its own transport), so there is nothing to close. Kept for
        # symmetry with EntraTokenProvider so app shutdown treats both alike.
        self._credentials.clear()

    def reset(self) -> None:
        """Drop cached credentials. For tests."""
        self._credentials.clear()
        self._locks.clear()
