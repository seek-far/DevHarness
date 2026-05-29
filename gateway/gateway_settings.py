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

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / f"gateway_{_probe.env}.env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


gateway_config = GatewaySettings()
