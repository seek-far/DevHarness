"""
GatewaySettings: two-step loading, same style as orchestrator_settings.

Step 1 — _EnvProbe: reads the ENV field, gets the current environment name.
Step 2 — GatewaySettings: loads gateway_<env>.env by environment name.

gateway_stream must match OrchestratorSettings.gateway_stream.
"""
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent


class _EnvProbe(BaseSettings):
    env: str = "local_multi_process"
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        extra="ignore",
    )


_probe = _EnvProbe()


class GatewaySettings(BaseSettings):
    env: str = _probe.env
    use_redis: bool = True
    redis_url: str = "redis://localhost:6379/0"

    # Must match gateway_stream in settings/base_settings.py
    gateway_stream: str = "gateway:stream"

    # Cap on `gateway:stream` length, enforced on every XADD via Redis's
    # `MAXLEN ~ N` (approximate) trim. Without this the stream grows
    # forever — XLEN includes ACKed entries because Redis Streams don't
    # auto-trim on ACK. A long-running gateway will eventually OOM Redis
    # (verified in stress_test_1: 19 bugs left 313 entries in the stream
    # after the run; multiply by uptime). The default 10000 gives ~500x
    # headroom over the largest observed burst (38 events), so a healthy
    # orchestrator never sees trim of un-ACKed PEL entries — trim only
    # touches already-consumed history. Lower it on memory-constrained
    # deployments; never set to 0 (= unlimited).
    gateway_stream_maxlen: int = 10000

    # --- webhook authentication / authorization (additive; default off) ---
    #
    # ONE switch drives both Step 1 (authn) and Step 2 (authz):
    #
    #   none — the pre-auth behaviour, byte-identical. `/webhook` accepts any
    #          POST. This is what every existing harness runs, so it stays the
    #          default; turning auth on is an operator decision.
    #   oidc — the caller must present a GitLab-signed CI id_token whose
    #          `project_id` claim equals the payload's `project.id`.
    #
    # There is deliberately NO separate "authenticate but don't authorize"
    # mode. Verifying the signature and then trusting the payload's project id
    # would leave the actual hole open (a valid token from project A could
    # still trigger a run against project B), while looking secured.
    #
    # Contract + the .gitlab-ci.yml side: docs/auth.md, infra/oidc-webhook/.
    webhook_auth_mode: str = "none"

    # GitLab instance root, e.g. https://gitlab.com — must match the token's
    # `iss` exactly. Empty while mode=oidc is a fatal misconfiguration, never
    # a fallback to open.
    oidc_issuer: str = ""

    # Must match the `aud` in .gitlab-ci.yml's `id_tokens:`. GitLab mints a
    # token for whatever audience a job asks for, so an unchecked audience
    # means any project on the instance can forge a trigger. No default —
    # a guessable one (e.g. "sdlcma") would defeat the purpose.
    oidc_audience: str = ""

    # Optional override; derived as {issuer}/oauth/discovery/keys when empty.
    oidc_jwks_url: str = ""

    # How long a fetched JWKS stays usable. GitLab rotates rarely; an unknown
    # kid triggers a refetch regardless, so this is a staleness bound, not a
    # rotation lag.
    oidc_jwks_cache_seconds: int = 300

    # Floor between forced JWKS refetches. Guards the amplification path: an
    # unauthenticated caller sending random `kid`s would otherwise cost one
    # HTTPS round-trip to GitLab per request.
    oidc_jwks_min_refresh_seconds: int = 60

    # Clock-skew tolerance for exp/nbf/iat. GitLab CI id_tokens are short
    # lived (~5 min), so this stays small.
    oidc_leeway_seconds: int = 30

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / f"gateway_{_probe.env}.env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


gateway_config = GatewaySettings()
