import logging
from typing import Dict, Optional

from .models import WorkerEntry, WorkerStatus

logger = logging.getLogger(__name__)


class WorkerRegistry:
    """
    Worker registry.
    Runs in a single event loop; all operations complete between awaits,
    so there are no concurrent writes and no locks are needed.
    """

    def __init__(self):
        self._workers: Dict[str, WorkerEntry] = {}

    def register(self, entry: WorkerEntry) -> None:
        self._workers[entry.bug_id] = entry
        logger.info(f"[Registry] registered bug_id={entry.bug_id} pid={entry.pid}")

    def get(self, bug_id: str) -> Optional[WorkerEntry]:
        return self._workers.get(bug_id)

    def update_status(self, bug_id: str, status: WorkerStatus) -> None:
        entry = self._workers.get(bug_id)
        if entry:
            entry.status = status
            logger.info(f"[Registry] bug_id={bug_id} status -> {status}")

    def remove(self, bug_id: str) -> None:
        if bug_id in self._workers:
            del self._workers[bug_id]
            logger.info(f"[Registry] removed bug_id={bug_id}")

    def all_active(self) -> Dict[str, WorkerEntry]:
        return {k: v for k, v in self._workers.items() if v.status in ("warmup", "running")}

    def exists(self, bug_id: str) -> bool:
        entry = self._workers.get(bug_id)
        return entry is not None and entry.status in ("warmup", "running")
