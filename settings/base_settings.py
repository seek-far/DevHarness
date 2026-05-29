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

    # ── Worker-completion signal (orchestrator's restart suppression) ─
    # The worker SETs this key right before exiting (any outcome: fixed,
    # no_fix, error, R10-short-circuit). The HealthMonitor reads it before
    # restarting on heartbeat expiry — without this, a successfully-exited
    # worker can look identical to a crashed one for the ~30-60s window
    # between worker exit and the container runtime reporting STOPPED back
    # via the spawner (real ECS bug observed 2026-05-21: 37 misfires after
    # MR !6 opened, see project memory). TTL is long enough that the
    # Monitor's next 20s sweep always catches it.
    worker_completed_key: str = "worker:completed:{bug_id}"
    worker_completed_ttl: int = 86400    # seconds (1 day)

    # ── Health Monitor ────────────────────────────────────────
    health_check_interval: int = 20      # seconds
    # Grace period before a terminally-statused (done/failed) worker
    # entry is removed from WorkerRegistry. Long enough for a late
    # ValidationStatusEvent (CI for the fix-branch the worker just
    # pushed) to still find the bug_id and log a meaningful
    # "no active worker" rather than a generic miss. 60s = ~3 health
    # check intervals, comfortably > heartbeat_ttl. Without this
    # sweep, terminal entries accumulate in _workers forever — a slow
    # memory leak verified live 2026-05-29.
    worker_registry_done_grace_seconds: int = 60

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
