"""
BaseAppSettings: shared field definitions + defaults for all environments.

Subclasses only need to override fields that change, or inject via .env files.
Field names match the old config.py constant names (lowercase),
callers use attribute access with behavior identical to the original constants.
"""
from pydantic import field_validator
from pydantic_settings import BaseSettings


class BaseAppSettings(BaseSettings):
    # ── Environment identifier ────────────────────────────────
    env: str = "local"

    # ── Redis connection ──────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── Gateway Stream ────────────────────────────────────────
    gateway_stream: str = "gateway:stream"
    gateway_consumer_group: str = "orchestrator-group"
    gateway_consumer_name: str = "orchestrator-0"

    # ── Worker Inbox Stream ───────────────────────────────────
    # Contains {bug_id} placeholder, expanded at runtime with .format(bug_id=…)
    worker_inbox_stream_key: str = "worker:{bug_id}:stream"
    worker_inbox_group: str = "worker-group"
    worker_inbox_consumer: str = "worker-0"

    # ── Dead-letter Stream ────────────────────────────────────
    dead_letter_stream: str = "orchestrator:dead_letter"

    # ── Heartbeat ─────────────────────────────────────────────
    worker_heartbeat_key: str = "worker:heartbeat:{bug_id}"
    worker_heartbeat_interval: int = 10  # seconds
    worker_heartbeat_ttl: int = 30       # seconds

    # ── Health Monitor ────────────────────────────────────────
    health_check_interval: int = 20      # seconds

    # ── Stream reading ────────────────────────────────────────
    stream_block_ms: int = 1000          # XREADGROUP BLOCK timeout (milliseconds)
    stream_count: int = 10               # max entries to read per iteration

    # ── Docker mode (local_docker_compose) ─────────────────────
    worker_image: str = "dh-bf-worker:latest"
    docker_network: str = "sdlcma_net"
    ssh_private_key: str = ""  # SSH private key content, injected into worker containers

    # ── Validation ────────────────────────────────────────────
    @field_validator("worker_heartbeat_ttl")
    @classmethod
    def ttl_gt_interval(cls, v: int, info) -> int:
        interval = (info.data or {}).get("worker_heartbeat_interval", 5)
        if v <= interval:
            raise ValueError(
                f"worker_heartbeat_ttl ({v}) must be > worker_heartbeat_interval ({interval})"
            )
        return v
