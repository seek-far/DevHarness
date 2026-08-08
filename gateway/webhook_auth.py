"""OIDC authentication + project authorization for `POST /webhook`.

This is Step 1 (authn) and Step 2 (authz) of the gateway's 3A story, and both
hang off ONE additive switch: `GatewaySettings.webhook_auth_mode`.

    none (default) — no-op. The webhook path stays byte-identical to the
                     pre-auth gateway, so all seven existing deployment
                     harnesses keep working untouched.
    oidc           — the caller must present a GitLab-signed OIDC JWT, AND
                     that token's `project_id` claim must equal the
                     `project.id` in the payload it is trying to submit.

WHY OIDC AND NOT A WEBHOOK SECRET
---------------------------------
GitLab's native webhook auth is `X-Gitlab-Token`, a shared secret compared in
plaintext. It answers "does the caller know the secret", which is enough to
stop anonymous abuse but useless for authorization: the `project.id` in the
body is still an attacker-controlled string, and the worker clones the URL
that body names using OUR GitLab token.

A GitLab CI id_token moves `project_id` from the body into a signed claim.
That is the whole point of Step 2 — the authorization decision is made against
something the issuer asserted, not against something the caller typed. A
legitimate token minted by project A can no longer trigger a run against
project B.

The trigger therefore inverts: instead of GitLab's webhook subsystem POSTing
to us, the failing CI job POSTs to us, carrying a token it obtained via
`id_tokens:` in `.gitlab-ci.yml`. See `infra/oidc-webhook/` for the snippet.

WHAT IS VERIFIED
----------------
  signature   RS256 only, against the issuer's JWKS.
  iss         must equal the configured issuer exactly.
  aud         must equal the configured audience exactly — this is what stops
              a token minted for some OTHER relying party (a cloud provider,
              a registry) from being replayed at us. GitLab will happily mint
              a token for any `aud` a `.gitlab-ci.yml` asks for, so an
              unchecked `aud` means any project on the instance can forge a
              trigger.
  exp/nbf/iat standard time validation with a small configurable leeway.
  project_id  Step 2: claim must equal payload `project.id`.

⚠️ `algorithms` is hardcoded to RS256 and is NOT configurable. Letting an
operator widen it is how the classic JWT confusion attacks land: `alg: none`
(no signature at all) and `alg: HS256` (verify an RSA *public* key as if it
were an HMAC secret — and a JWKS public key is, by definition, public). PyJWT
refuses the mismatch only because we pin the list here.

JWKS FETCHING
-------------
`PyJWKClient` caches the key set for `oidc_jwks_cache_seconds` and, on a kid
it does not recognise, refetches once — that refetch is what makes key
rotation transparent. It is also an unauthenticated amplification vector:
every request bearing a made-up `kid` would cost one HTTPS round-trip to the
GitLab instance we are trying to protect. `_JwksResolver` therefore checks the
cached set FIRST and rate-limits the forced refresh to one per
`oidc_jwks_min_refresh_seconds`; unknown kids inside that window are rejected
without touching the network.

Everything here is synchronous and side-effect-free apart from the JWKS
cache, so tests call it directly. The gateway runs it in a worker thread
(`asyncio.to_thread`) because a cold JWKS fetch is a blocking HTTPS call.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# GitLab signs CI id_tokens with RS256. Deliberately not configurable — see
# the module docstring.
_ALGORITHMS = ["RS256"]

# GitLab's JWKS lives at a fixed path under the instance root. The OIDC
# discovery document (`/.well-known/openid-configuration`) points here; we
# skip the discovery hop because the path has been stable for years and one
# fewer network dependency at request time is worth more than the indirection.
DEFAULT_JWKS_PATH = "/oauth/discovery/keys"

# Claims we refuse to run without. `sub` is not required: GitLab sets it to
# `project_path:group/proj:ref_type:branch:ref:main`, which is useful for
# audit but is not what we authorize on.
_REQUIRED_CLAIMS = ["exp", "iat", "aud", "iss", "project_id"]


class WebhookAuthError(Exception):
    """401 — the caller failed to prove a valid identity."""

    def __init__(self, message: str, reason: str = "invalid_token") -> None:
        super().__init__(message)
        self.reason = reason


class WebhookAuthzError(Exception):
    """403 — the identity is valid but may not trigger the named project."""

    def __init__(self, message: str, reason: str = "project_mismatch") -> None:
        super().__init__(message)
        self.reason = reason


class WebhookAuthConfigError(Exception):
    """500 — `webhook_auth_mode=oidc` without the settings it needs.

    Deliberately NOT a silent fallback to open. An operator who turns auth on
    and mistypes the issuer must get an outage, not an unauthenticated
    gateway that looks configured.
    """

    def __init__(self, message: str, reason: str = "misconfigured") -> None:
        super().__init__(message)
        self.reason = reason


def jwks_url_for(cfg: Any) -> str:
    """Explicit `oidc_jwks_url`, else derived from the issuer."""
    explicit = (getattr(cfg, "oidc_jwks_url", "") or "").strip()
    if explicit:
        return explicit
    issuer = (getattr(cfg, "oidc_issuer", "") or "").strip()
    return issuer.rstrip("/") + DEFAULT_JWKS_PATH


class _JwksResolver:
    """Per-JWKS-URL signing-key lookup with a throttled rotation refetch."""

    def __init__(self, jwks_url: str, cache_seconds: int, min_refresh_seconds: int) -> None:
        self.jwks_url = jwks_url
        self.cache_seconds = cache_seconds
        self.min_refresh_seconds = min_refresh_seconds
        self._client: Any = None
        self._lock = threading.Lock()
        # -inf semantics: the first unknown kid is always allowed to refetch.
        self._last_forced_refresh = float("-inf")

    def _client_or_raise(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from jwt import PyJWKClient
        except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
            raise WebhookAuthConfigError(
                "webhook_auth_mode=oidc needs the `PyJWT[crypto]` package "
                "(pip install 'PyJWT[crypto]')."
            ) from exc
        self._client = PyJWKClient(
            self.jwks_url,
            cache_jwk_set=True,
            lifespan=self.cache_seconds,
        )
        return self._client

    def signing_key(self, token: str, kid: str | None) -> Any:
        client = self._client_or_raise()

        # Fast path: the kid is in the cached set. A cold cache fetches once
        # here, which is the normal startup cost, not the abuse path.
        if kid:
            try:
                cached = client.get_signing_keys(refresh=False)
                match = client.match_kid(cached, kid)
                if match is not None:
                    return match
            except Exception:
                # A broken/empty cache is not fatal — fall through to the
                # throttled refresh, which will surface the real error.
                pass

        with self._lock:
            now = time.monotonic()
            if now - self._last_forced_refresh < self.min_refresh_seconds:
                raise WebhookAuthError(
                    f"unrecognised signing key {kid!r} and the JWKS refresh is "
                    "rate-limited; retry shortly",
                    reason="unknown_key",
                )
            self._last_forced_refresh = now

        try:
            return client.get_signing_key_from_jwt(token)
        except WebhookAuthError:
            raise
        except Exception as exc:
            raise WebhookAuthError(
                f"could not resolve a signing key from {self.jwks_url}: {exc}",
                reason="unknown_key",
            ) from exc


# One resolver per JWKS URL, process-wide. Keyed by URL rather than held on
# the config object because the config is rebuilt on reload while the key
# cache should survive it.
_RESOLVERS: dict[str, _JwksResolver] = {}
_RESOLVERS_LOCK = threading.Lock()


def _resolver_for(cfg: Any) -> _JwksResolver:
    url = jwks_url_for(cfg)
    cache_seconds = int(getattr(cfg, "oidc_jwks_cache_seconds", 300) or 300)
    min_refresh = int(getattr(cfg, "oidc_jwks_min_refresh_seconds", 60) or 0)
    with _RESOLVERS_LOCK:
        resolver = _RESOLVERS.get(url)
        if resolver is None:
            resolver = _JwksResolver(url, cache_seconds, min_refresh)
            _RESOLVERS[url] = resolver
        return resolver


def reset_resolvers() -> None:
    """Drop every cached JWKS client. For tests and config reloads."""
    with _RESOLVERS_LOCK:
        _RESOLVERS.clear()


def extract_bearer(authorization: str | None) -> str:
    """`Authorization: Bearer <jwt>` → `<jwt>`.

    Scheme match is case-insensitive per RFC 7235; the token itself is not
    touched.
    """
    if not authorization or not authorization.strip():
        raise WebhookAuthError(
            "missing Authorization header; webhook_auth_mode=oidc requires "
            "`Authorization: Bearer <gitlab-ci-id-token>`",
            reason="missing_token",
        )
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise WebhookAuthError(
            "Authorization header is not a Bearer token",
            reason="malformed_token",
        )
    return parts[1].strip()


def _validate_config(cfg: Any) -> None:
    issuer = (getattr(cfg, "oidc_issuer", "") or "").strip()
    audience = (getattr(cfg, "oidc_audience", "") or "").strip()
    missing = [
        name
        for name, value in (("OIDC_ISSUER", issuer), ("OIDC_AUDIENCE", audience))
        if not value
    ]
    if missing:
        raise WebhookAuthConfigError(
            "webhook_auth_mode=oidc requires " + " and ".join(missing) +
            " to be set; refusing to accept webhooks rather than fall back to "
            "unauthenticated."
        )


def verify_token(token: str, cfg: Any) -> dict:
    """Verify signature + registered claims. Returns the decoded claims."""
    import jwt as _jwt

    _validate_config(cfg)

    try:
        header = _jwt.get_unverified_header(token)
    except Exception as exc:
        raise WebhookAuthError(f"malformed JWT: {exc}", reason="malformed_token") from exc

    signing_key = _resolver_for(cfg).signing_key(token, header.get("kid"))
    key = getattr(signing_key, "key", signing_key)

    leeway = int(getattr(cfg, "oidc_leeway_seconds", 30) or 0)
    try:
        claims = _jwt.decode(
            token,
            key,
            algorithms=_ALGORITHMS,
            audience=(getattr(cfg, "oidc_audience", "") or "").strip(),
            issuer=(getattr(cfg, "oidc_issuer", "") or "").strip(),
            leeway=leeway,
            options={"require": _REQUIRED_CLAIMS},
        )
    except _jwt.ExpiredSignatureError as exc:
        raise WebhookAuthError(f"token expired: {exc}", reason="expired_token") from exc
    except _jwt.InvalidAudienceError as exc:
        raise WebhookAuthError(
            f"token audience does not match {getattr(cfg, 'oidc_audience', '')!r}: {exc}",
            reason="invalid_audience",
        ) from exc
    except _jwt.InvalidIssuerError as exc:
        raise WebhookAuthError(
            f"token issuer does not match {getattr(cfg, 'oidc_issuer', '')!r}: {exc}",
            reason="invalid_issuer",
        ) from exc
    except _jwt.InvalidSignatureError as exc:
        raise WebhookAuthError(f"bad signature: {exc}", reason="invalid_signature") from exc
    except _jwt.MissingRequiredClaimError as exc:
        raise WebhookAuthError(f"missing claim: {exc}", reason="invalid_claims") from exc
    except _jwt.InvalidTokenError as exc:
        # Catch-all for the rest of PyJWT's tree, incl. InvalidAlgorithmError
        # (an `alg: none` / `alg: HS256` confusion attempt lands here).
        raise WebhookAuthError(f"invalid token: {exc}", reason="invalid_token") from exc

    if not isinstance(claims, dict):  # pragma: no cover - PyJWT always returns a dict
        raise WebhookAuthError("decoded token is not a claim set", reason="invalid_claims")
    return claims


def _as_id(value: Any) -> str:
    """Normalise a project id for comparison.

    The claim is a string (`"42"`), the webhook payload carries an int (`42`).
    Comparing them raw is a silent always-false, which would look exactly like
    a working authorization check while rejecting every legitimate call.
    """
    if value is None:
        return ""
    if isinstance(value, bool):  # bools are ints in Python; never a project id
        return ""
    return str(value).strip()


def authorize_project(claims: dict, payload: Any) -> str:
    """Step 2: the token's `project_id` claim must equal payload `project.id`.

    Both sides must be present. A payload without `project.id` is rejected
    rather than waved through — under `oidc` mode an unauthorizable request is
    a failed request, because the worker will clone whatever URL that payload
    names.
    """
    claim_pid = _as_id(claims.get("project_id"))
    if not claim_pid:
        raise WebhookAuthzError(
            "token carries no usable project_id claim", reason="project_mismatch"
        )

    project = payload.get("project") if isinstance(payload, dict) else None
    payload_pid = _as_id(project.get("id") if isinstance(project, dict) else None)
    if not payload_pid:
        raise WebhookAuthzError(
            "payload carries no project.id to authorize against",
            reason="project_mismatch",
        )

    if claim_pid != payload_pid:
        raise WebhookAuthzError(
            f"token is scoped to project {claim_pid} but the payload targets "
            f"project {payload_pid}",
            reason="project_mismatch",
        )
    return claim_pid


def authenticate_and_authorize(authorization: str | None, payload: Any, cfg: Any) -> dict:
    """Full gate: bearer → verified claims → project match. Returns claims.

    Raises WebhookAuthError (401), WebhookAuthzError (403) or
    WebhookAuthConfigError (500). Callers map `.reason` onto the rejection
    metric — the set is a closed enum, so it is a safe Prometheus label.
    """
    token = extract_bearer(authorization)
    claims = verify_token(token, cfg)
    authorize_project(claims, payload)
    return claims
