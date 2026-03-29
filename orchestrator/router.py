"""
MessageRouter: uses XADD to write to the worker inbox stream, replacing the original RPUSH List.
"""

import json
import logging

from redis.asyncio import Redis

from .models import ValidationStatusEvent
from .registry import WorkerRegistry

logger = logging.getLogger(__name__)


class MessageRouter:
    def __init__(self, registry: WorkerRegistry, redis: Redis, inbox_stream_tpl: str):
        self._registry = registry
        self._redis = redis
        self._inbox_stream_tpl = inbox_stream_tpl

    async def route(self, event: ValidationStatusEvent) -> bool:
        entry = self._registry.get(event.bug_id)
        if not entry or entry.status not in ("warmup", "running"):
            logger.warning("[Router] no active worker for bug_id=%s", event.bug_id)
            return False

        inbox_stream = self._inbox_stream_tpl.format(bug_id=event.bug_id)
        # XADD writes to the worker inbox stream; "*" lets Redis auto-generate the entry ID
        await self._redis.xadd(inbox_stream, {"data": json.dumps(event.raw)})
        logger.info(
            "[Router] routed to stream=%r status=%s", inbox_stream, event.status
        )
        return True
