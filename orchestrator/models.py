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


@dataclass
class ValidationStatusEvent:
    bug_id: str
    status: str
    raw: dict

@dataclass
class OtherEvent:
    raw: dict

WARMUP_GRACE = 120  # seconds to wait for first heartbeat before declaring failure

WorkerStatus = Literal["warmup", "running", "failed", "done"]


@dataclass
class WorkerEntry:
    bug_id: str
    process: Any  # asyncio.subprocess.Process or DockerProcessProxy
    project_id: str = ""
    project_web_url: str = ""
    job_id: str = ""
    started_at: float = field(default_factory=time.time)
    warmup_deadline: float = 0.0
    restart_count: int = 0
    status: WorkerStatus = "warmup"

    @property
    def pid(self) -> Optional[Union[int, str]]:
        return self.process.pid if self.process else None
