import logging
import time
from typing import Dict, Optional

from .models import WorkerEntry, WorkerStatus

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES: tuple[WorkerStatus, ...] = ("done", "failed")


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
            # Stamp `done_at` on the FIRST terminal transition only —
            # never reset to a later wallclock if the same entry somehow
            # gets re-stamped (the grace period should measure age since
            # initial terminal, not since last touch). getattr fallback
            # tolerates SimpleNamespace test fakes that lack the field.
            if status in _TERMINAL_STATUSES and getattr(entry, "done_at", None) is None:
                try:
                    entry.done_at = time.time()
                except AttributeError:
                    # Fake entry without a writable done_at slot — fine,
                    # those entries are exempt from sweep_stale anyway
                    # because sweep_stale also requires done_at to be set.
                    pass
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

    def sweep_stale(self, grace_seconds: float) -> int:
        """Drop done/failed entries older than `grace_seconds`. Returns
        the number actually removed.

        Why a grace period: a late ValidationStatusEvent (CI for the
        fix-branch that an exiting worker just pushed) might still need
        to look up bug_id in the registry to route correctly. The grace
        is the window where the entry stays around for that lookup
        AFTER status transitioned to terminal.

        Entries with `done_at = None` are NEVER removed by this method,
        even if their status is somehow terminal — that would mean a
        terminal transition happened without going through
        update_status() (impossible today, but defending against future
        refactors that bypass it).
        """
        now = time.time()
        removed = 0
        for bug_id in list(self._workers.keys()):
            entry = self._workers[bug_id]
            if entry.status not in _TERMINAL_STATUSES:
                continue
            done_at = getattr(entry, "done_at", None)
            if done_at is None:
                continue
            if now - done_at <= grace_seconds:
                continue
            del self._workers[bug_id]
            removed += 1
            logger.info(
                "[Registry] swept stale bug_id=%s status=%s age=%.0fs",
                bug_id, entry.status, now - entry.done_at,
            )
        return removed
