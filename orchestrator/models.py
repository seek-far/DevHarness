from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Union
import asyncio
import time


@dataclass
class BugReportedEvent:
    #bug_id: str
    project_id: str
    project_web_url: str
    job_id: str
    raw: dict
    # The branch the failed pipeline ran on (payload["object_attributes"]["ref"]).
    # Empty string preserves the historical default ("main") downstream when
    # an older payload / test omits the ref.
    source_branch: str = ""


@dataclass
class ValidationStatusEvent:
    bug_id: str
    status: str
    raw: dict

@dataclass
class OtherEvent:
    raw: dict

WARMUP_GRACE = 120  # seconds to wait for first heartbeat before declaring failure

# How many times HealthMonitor will re-spawn one bug's worker before giving up.
# A cap is not optional: both restart triggers (heartbeat expiry, abnormal
# process exit) fire again on the replacement, so a worker that dies
# deterministically — a bad patch that segfaults the test suite, an instance
# whose container will not start — would otherwise be re-spawned forever,
# burning LLM budget and a worker slot with nothing to show for it.
MAX_WORKER_RESTARTS = 3

WorkerStatus = Literal["warmup", "running", "failed", "done"]


@dataclass
class WorkerEntry:
    bug_id: str
    process: Any  # asyncio.subprocess.Process or DockerProcessProxy
    project_id: str = ""
    project_web_url: str = ""
    job_id: str = ""
    # Source branch the failed pipeline ran on. Stored so HealthMonitor
    # restarts re-spawn the worker against the same base (Item 3).
    # Empty string falls back to "main" in the worker.
    source_branch: str = ""
    started_at: float = field(default_factory=time.time)
    warmup_deadline: float = 0.0
    restart_count: int = 0
    status: WorkerStatus = "warmup"
    # Wallclock when status first became terminal (done | failed). None
    # while still warmup/running. Used by WorkerRegistry.sweep_stale()
    # to age out terminal entries after a grace period so the registry
    # doesn't grow unboundedly across a long-running orchestrator.
    done_at: Optional[float] = None

    @property
    def pid(self) -> Optional[Union[int, str]]:
        return self.process.pid if self.process else None
