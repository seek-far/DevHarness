"""OIDC webhook authentication (Step 1) + project authorization (Step 2).

Both hang off ONE additive switch, `GatewaySettings.webhook_auth_mode`:

    none (default)  →  the pre-auth gateway, byte-identical
    oidc            →  GitLab-signed CI id_token required, AND its
                       `project_id` claim must equal the payload's project.id

What these tests pin, in rough order of "what breaks the security property":

  * mode=none is genuinely a no-op (every existing harness depends on it).
  * A rejected request never reaches Redis — no XADD, so no worker, so no
    LLM spend. This is the actual thing being defended.
  * Step 2 rejects a VALID token aimed at someone else's project. This is
    the reason for OIDC over a shared webhook secret: the project id is
    asserted by the issuer, not typed by the caller.
  * The int-vs-str normalisation. GitLab puts `project_id` in the claim as a
    string and `project.id` in the payload as an int; comparing them raw is a
    silent always-false that looks like a working check while rejecting every
    legitimate call.
  * Algorithm confusion (`alg: none`, `alg: HS256` against the RSA public
    key) is refused. A JWKS public key is public by definition, so an HS256
    path would let anyone mint tokens.
  * Misconfiguration fails CLOSED (500), never open.
  * The unknown-kid JWKS refetch is rate-limited, so an unauthenticated
    caller cannot use us to hammer the GitLab instance we protect.

The network is stubbed at PyJWKClient.fetch_data, so PyJWT's real caching,
kid-matching and rotation logic stays under test — only the HTTPS call is
replaced.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

jwt = pytest.importorskip("jwt")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from jwt import PyJWKClient  # noqa: E402
from jwt.algorithms import RSAAlgorithm  # noqa: E402

from gateway import gateway as gw_mod  # noqa: E402
from gateway import metrics, webhook_auth  # noqa: E402

ISSUER = "https://gitlab.example.com"
AUDIENCE = "https://sdlcma-gateway.example.com"
KID = "sdlcma-test-key-1"
PROJECT_ID = 4242


# --------------------------------------------------------------------------
# key material — generated once per session (RSA keygen is slow)
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


@pytest.fixture(scope="session")
def jwks(keypair):
    _priv, pub = keypair
    entry = json.loads(RSAAlgorithm.to_jwk(pub))
    entry.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return {"keys": [entry]}


@pytest.fixture(scope="session")
def other_private_key():
    """A second key, never published in the JWKS — for forged signatures."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


# --------------------------------------------------------------------------
# token + payload builders
# --------------------------------------------------------------------------

def make_token(private_key, *, kid=KID, iss=ISSUER, aud=AUDIENCE,
               project_id=PROJECT_ID, exp_delta=300, drop=(), extra=None):
    now = int(time.time())
    claims = {
        "iss": iss,
        "aud": aud,
        "iat": now,
        "nbf": now,
        "exp": now + exp_delta,
        # GitLab emits project_id as a STRING even though the webhook payload
        # carries an int. Mirrored here on purpose.
        "project_id": str(project_id),
        "project_path": "grp/proj",
        "sub": "project_path:grp/proj:ref_type:branch:ref:main",
        "ref": "main",
    }
    for key in drop:
        claims.pop(key, None)
    if extra:
        claims.update(extra)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


def pipeline_payload(project_id=PROJECT_ID, *, drop_project=False):
    payload = {
        "object_kind": "pipeline",
        "object_attributes": {"ref": "main", "status": "failed"},
        "project": {"id": project_id, "web_url": "https://gitlab.example.com/grp/proj"},
        "builds": [{"id": 99}],
    }
    if drop_project:
        payload.pop("project")
    return payload


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unsigned_token(alg="none", **claim_overrides):
    """Hand-rolled token with an attacker-chosen `alg`; PyJWT will not emit
    some of these, which is the point."""
    now = int(time.time())
    header = {"alg": alg, "typ": "JWT", "kid": KID}
    claims = {
        "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 300,
        "project_id": str(PROJECT_ID),
    }
    claims.update(claim_overrides)
    return (
        _b64(json.dumps(header).encode())
        + "." + _b64(json.dumps(claims).encode())
        + "."
    )


# --------------------------------------------------------------------------
# app fixtures
# --------------------------------------------------------------------------

def _cfg(**overrides):
    base = dict(
        use_redis=True,
        gateway_stream="gateway:stream",
        gateway_stream_maxlen=1234,
        webhook_auth_mode="oidc",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        oidc_jwks_url="",
        oidc_jwks_cache_seconds=300,
        oidc_jwks_min_refresh_seconds=60,
        oidc_leeway_seconds=30,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeResponse(io.BytesIO):
    """urlopen's context-manager contract, nothing more."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fetch_counter(monkeypatch, jwks):
    """Stub the JWKS HTTPS call; count how often it actually fires.

    Patched at `urlopen`, NOT at `PyJWKClient.fetch_data` — fetch_data is
    where PyJWT writes its own cache, so stubbing it out defeats the caching
    this file is trying to measure and every request looks like a fetch.
    """
    calls = {"n": 0}

    def fake_urlopen(*_args, **_kwargs):
        calls["n"] += 1
        return _FakeResponse(json.dumps(jwks).encode())

    monkeypatch.setattr(jwt.jwks_client.urllib.request, "urlopen", fake_urlopen)
    return calls


@pytest.fixture
def client_factory(fetch_counter):
    made = []

    def _make(**cfg_overrides):
        fake_redis = MagicMock()
        cfg = _cfg(**cfg_overrides)
        gw_mod.override(cfg, fake_redis)
        webhook_auth.reset_resolvers()
        client = TestClient(gw_mod.app, raise_server_exceptions=False)
        made.append(client)
        return client, fake_redis, cfg

    yield _make
    gw_mod.override(None, None)
    webhook_auth.reset_resolvers()


def rejected(reason: str) -> float:
    return metrics.WEBHOOK_AUTH_REJECTED.labels(reason=reason)._value.get()


# --------------------------------------------------------------------------
# Step 0 — the additive contract
# --------------------------------------------------------------------------

def test_default_mode_is_none():
    """The switch must default OFF. Seven deployment harnesses POST to
    /webhook with no Authorization header today."""
    from gateway.gateway_settings import GatewaySettings
    assert GatewaySettings.model_fields["webhook_auth_mode"].default == "none"


def test_mode_none_is_a_true_noop(client_factory):
    """No header, no token, no config — still 200 and still XADDed."""
    client, fake_redis, _ = client_factory(
        webhook_auth_mode="none", oidc_issuer="", oidc_audience=""
    )
    r = client.post("/webhook", json=pipeline_payload())
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    fake_redis.xadd.assert_called_once()


def test_absent_setting_behaves_as_none(client_factory):
    """A config object predating this feature (no webhook_auth_mode field at
    all) must not crash — `getattr` default is the compatibility seam."""
    fake_redis = MagicMock()
    gw_mod.override(
        SimpleNamespace(
            use_redis=True, gateway_stream="gateway:stream", gateway_stream_maxlen=10
        ),
        fake_redis,
    )
    client = TestClient(gw_mod.app)
    assert client.post("/webhook", json=pipeline_payload()).status_code == 200
    fake_redis.xadd.assert_called_once()


# --------------------------------------------------------------------------
# Step 1 — authentication
# --------------------------------------------------------------------------

def test_valid_token_is_accepted_and_forwarded(client_factory, keypair):
    priv, _ = keypair
    client, fake_redis, _ = client_factory()
    r = client.post(
        "/webhook",
        json=pipeline_payload(),
        headers={"Authorization": f"Bearer {make_token(priv)}"},
    )
    assert r.status_code == 200
    fake_redis.xadd.assert_called_once()


def test_missing_header_is_401_and_never_reaches_redis(client_factory):
    before = rejected("missing_token")
    client, fake_redis, _ = client_factory()
    r = client.post("/webhook", json=pipeline_payload())
    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate") == "Bearer"
    # The property that matters: no XADD → no worker → no LLM spend.
    fake_redis.xadd.assert_not_called()
    assert rejected("missing_token") == before + 1


@pytest.mark.parametrize("header", ["", "   ", "Basic abc", "Bearer", "Bearer   "])
def test_malformed_authorization_headers_are_refused(client_factory, header):
    client, fake_redis, _ = client_factory()
    r = client.post(
        "/webhook", json=pipeline_payload(), headers={"Authorization": header}
    )
    assert r.status_code == 401
    fake_redis.xadd.assert_not_called()


def test_bearer_scheme_is_case_insensitive(client_factory, keypair):
    """RFC 7235 says the scheme is case-insensitive; some CI curl wrappers
    lowercase it."""
    priv, _ = keypair
    client, fake_redis, _ = client_factory()
    r = client.post(
        "/webhook",
        json=pipeline_payload(),
        headers={"Authorization": f"bearer {make_token(priv)}"},
    )
    assert r.status_code == 200
    fake_redis.xadd.assert_called_once()


def test_expired_token_is_refused(client_factory, keypair):
    priv, _ = keypair
    before = rejected("expired_token")
    client, fake_redis, _ = client_factory()
    # Beyond exp AND beyond the 30s leeway.
    token = make_token(priv, exp_delta=-120)
    r = client.post(
        "/webhook", json=pipeline_payload(), headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 401
    fake_redis.xadd.assert_not_called()
    assert rejected("expired_token") == before + 1


def test_wrong_audience_is_refused(client_factory, keypair):
    """The single most important claim check. GitLab mints a token for
    whatever `aud` a .gitlab-ci.yml asks for, so without this any project on
    the instance could replay a token it obtained for some other relying
    party."""
    priv, _ = keypair
    before = rejected("invalid_audience")
    client, fake_redis, _ = client_factory()
    token = make_token(priv, aud="https://some-other-service")
    r = client.post(
        "/webhook", json=pipeline_payload(), headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 401
    fake_redis.xadd.assert_not_called()
    assert rejected("invalid_audience") == before + 1


def test_wrong_issuer_is_refused(client_factory, keypair):
    priv, _ = keypair
    before = rejected("invalid_issuer")
    client, fake_redis, _ = client_factory()
    token = make_token(priv, iss="https://gitlab.evil.example")
    r = client.post(
        "/webhook", json=pipeline_payload(), headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 401
    fake_redis.xadd.assert_not_called()
    assert rejected("invalid_issuer") == before + 1


def test_signature_from_an_unpublished_key_is_refused(client_factory, other_private_key):
    """Right kid, wrong key — the forgery a JWKS is supposed to stop."""
    client, fake_redis, _ = client_factory()
    token = make_token(other_private_key)
    r = client.post(
        "/webhook", json=pipeline_payload(), headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 401
    fake_redis.xadd.assert_not_called()


def test_missing_project_id_claim_is_refused(client_factory, keypair):
    """`project_id` is in the required-claims list precisely because Step 2
    cannot authorize without it."""
    priv, _ = keypair
    client, fake_redis, _ = client_factory()
    token = make_token(priv, drop=("project_id",))
    r = client.post(
        "/webhook", json=pipeline_payload(), headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 401
    fake_redis.xadd.assert_not_called()


# --------------------------------------------------------------------------
# Step 1 — algorithm confusion
# --------------------------------------------------------------------------

def test_alg_none_is_refused(client_factory):
    """An unsigned token with a KNOWN kid — so it gets past key lookup and is
    stopped by the pinned algorithm list, not by accident."""
    client, fake_redis, _ = client_factory()
    r = client.post(
        "/webhook",
        json=pipeline_payload(),
        headers={"Authorization": f"Bearer {unsigned_token(alg='none')}"},
    )
    assert r.status_code == 401
    fake_redis.xadd.assert_not_called()


def test_hs256_confusion_against_the_public_key_is_refused(client_factory, keypair, jwks):
    """The classic RS256→HS256 downgrade: sign with the RSA *public* key as an
    HMAC secret. JWKS keys are public, so if this worked anyone could mint
    tokens. PyJWT only refuses it because we pin algorithms=["RS256"]."""
    from cryptography.hazmat.primitives import serialization
    _priv, pub = keypair
    pub_pem = pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    now = int(time.time())
    # Hand-rolled rather than via jwt.encode: PyJWT refuses to *sign* with a
    # PEM key under HS256 (InvalidKeyError). An attacker has no such scruples,
    # so the forgery is assembled the way an attacker would.
    signing_input = (
        _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
        + "."
        + _b64(json.dumps({
            "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 300,
            "project_id": str(PROJECT_ID),
        }).encode())
    )
    sig = hmac.new(pub_pem, signing_input.encode(), hashlib.sha256).digest()
    forged = signing_input + "." + _b64(sig)

    client, fake_redis, _ = client_factory()
    r = client.post(
        "/webhook", json=pipeline_payload(), headers={"Authorization": f"Bearer {forged}"}
    )
    assert r.status_code == 401
    fake_redis.xadd.assert_not_called()


# --------------------------------------------------------------------------
# Step 2 — authorization (rides the same switch, no separate flag)
# --------------------------------------------------------------------------

def test_valid_token_for_another_project_is_403(client_factory, keypair):
    """THE Step-2 test. The token is perfectly valid — correct issuer,
    audience, signature, unexpired. It is simply scoped to project 4242 while
    the payload asks us to go work on project 9999. Without this check the
    worker would clone 9999 using our GitLab credentials."""
    priv, _ = keypair
    before = rejected("project_mismatch")
    client, fake_redis, _ = client_factory()
    r = client.post(
        "/webhook",
        json=pipeline_payload(project_id=9999),
        headers={"Authorization": f"Bearer {make_token(priv, project_id=4242)}"},
    )
    assert r.status_code == 403
    # 403 not 401: the identity is fine, the request is not permitted.
    assert "WWW-Authenticate" not in r.headers
    fake_redis.xadd.assert_not_called()
    assert rejected("project_mismatch") == before + 1


def test_payload_without_project_id_is_403(client_factory, keypair):
    """Unauthorizable means refused, not waved through."""
    priv, _ = keypair
    client, fake_redis, _ = client_factory()
    r = client.post(
        "/webhook",
        json=pipeline_payload(drop_project=True),
        headers={"Authorization": f"Bearer {make_token(priv)}"},
    )
    assert r.status_code == 403
    fake_redis.xadd.assert_not_called()


def test_string_claim_matches_int_payload(client_factory, keypair):
    """GitLab: claim `"4242"` vs payload `4242`. A raw `==` here is a silent
    always-false — it would reject every legitimate webhook while looking
    like a correctly-enforced policy."""
    priv, _ = keypair
    client, fake_redis, _ = client_factory()
    token = make_token(priv, project_id=4242)
    assert json.loads(jwt.decode(
        token, options={"verify_signature": False}
    )["project_id"]) == 4242  # the claim really is a string
    r = client.post(
        "/webhook",
        json=pipeline_payload(project_id=4242),   # …and the payload really is an int
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    fake_redis.xadd.assert_called_once()


def test_authorize_project_rejects_bool_ids():
    """`True == 1` in Python. A payload of `{"project": {"id": true}}` must
    not authorize against claim `"1"`."""
    with pytest.raises(webhook_auth.WebhookAuthzError):
        webhook_auth.authorize_project({"project_id": "1"}, {"project": {"id": True}})


# --------------------------------------------------------------------------
# fail-closed configuration
# --------------------------------------------------------------------------

@pytest.mark.parametrize("missing", [{"oidc_issuer": ""}, {"oidc_audience": ""}])
def test_oidc_without_required_settings_fails_closed(client_factory, keypair, missing):
    """Turning auth on with a half-filled config must be an outage, not an
    unauthenticated gateway that looks configured."""
    priv, _ = keypair
    before = rejected("misconfigured")
    client, fake_redis, _ = client_factory(**missing)
    r = client.post(
        "/webhook",
        json=pipeline_payload(),
        headers={"Authorization": f"Bearer {make_token(priv)}"},
    )
    assert r.status_code == 500
    fake_redis.xadd.assert_not_called()
    assert rejected("misconfigured") == before + 1


def test_unknown_mode_fails_closed(client_factory, keypair):
    """A typo (`odic`, `OIDC `, `true`) must not read as `none`."""
    priv, _ = keypair
    client, fake_redis, _ = client_factory(webhook_auth_mode="odic")
    r = client.post(
        "/webhook",
        json=pipeline_payload(),
        headers={"Authorization": f"Bearer {make_token(priv)}"},
    )
    assert r.status_code == 500
    fake_redis.xadd.assert_not_called()


def test_mode_is_case_and_space_tolerant(client_factory, keypair):
    priv, _ = keypair
    client, fake_redis, _ = client_factory(webhook_auth_mode="  OIDC ")
    r = client.post(
        "/webhook",
        json=pipeline_payload(),
        headers={"Authorization": f"Bearer {make_token(priv)}"},
    )
    assert r.status_code == 200
    fake_redis.xadd.assert_called_once()


# --------------------------------------------------------------------------
# JWKS handling
# --------------------------------------------------------------------------

def test_jwks_url_derived_from_issuer():
    cfg = _cfg(oidc_issuer="https://gitlab.example.com/", oidc_jwks_url="")
    assert webhook_auth.jwks_url_for(cfg) == (
        "https://gitlab.example.com/oauth/discovery/keys"
    )


def test_explicit_jwks_url_wins():
    cfg = _cfg(oidc_jwks_url="https://elsewhere/keys")
    assert webhook_auth.jwks_url_for(cfg) == "https://elsewhere/keys"


def test_unknown_kid_refetch_is_rate_limited(client_factory, keypair, fetch_counter):
    """An unauthenticated caller must not be able to turn one forged `kid`
    per request into one HTTPS round-trip per request against the GitLab
    instance we are protecting."""
    priv, _ = keypair
    client, fake_redis, _ = client_factory(oidc_jwks_min_refresh_seconds=3600)

    # First unknown kid: allowed to force one refetch (rotation support).
    r1 = client.post(
        "/webhook",
        json=pipeline_payload(),
        headers={"Authorization": f"Bearer {make_token(priv, kid='rotated-in')}"},
    )
    assert r1.status_code == 401
    after_first = fetch_counter["n"]

    # Next ones inside the window are refused without touching the network.
    for _ in range(5):
        r = client.post(
            "/webhook",
            json=pipeline_payload(),
            headers={"Authorization": f"Bearer {make_token(priv, kid='forged')}"},
        )
        assert r.status_code == 401
    assert fetch_counter["n"] == after_first, "throttle leaked JWKS fetches"
    fake_redis.xadd.assert_not_called()


def test_known_kid_does_not_refetch_per_request(client_factory, keypair, fetch_counter):
    """The warm path is cache-only: N valid webhooks must not be N JWKS
    fetches."""
    priv, _ = keypair
    client, _fake, _ = client_factory()
    for _ in range(4):
        r = client.post(
            "/webhook",
            json=pipeline_payload(),
            headers={"Authorization": f"Bearer {make_token(priv)}"},
        )
        assert r.status_code == 200
    assert fetch_counter["n"] <= 1


# --------------------------------------------------------------------------
# audit trail
# --------------------------------------------------------------------------

def test_accept_and_reject_emit_phase_markers(client_factory, keypair, caplog):
    """Auth decisions must be greppable by the same post-processor that
    rebuilds per-bug timelines — including the rejections, which never become
    a bug_id and would otherwise leave no trace."""
    import logging
    caplog.set_level(logging.INFO, logger="gateway.gateway")
    priv, _ = keypair
    client, _fake, _ = client_factory()

    client.post(
        "/webhook",
        json=pipeline_payload(),
        headers={"Authorization": f"Bearer {make_token(priv)}"},
    )
    client.post("/webhook", json=pipeline_payload())

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "phase_marker phase=webhook_auth result=accept" in text
    assert f"project_id={PROJECT_ID}" in text
    assert "phase_marker phase=webhook_auth result=reject reason=missing_token" in text
