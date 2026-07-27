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
        if entry and entry.status in ("warmup", "running"):
            # Existing path: worker registered in the in-memory WorkerRegistry
            # (subprocess / Docker / ECS / K8s spawner modes).
            inbox_stream = self._inbox_stream_tpl.format(bug_id=event.bug_id)
            await self._redis.xadd(inbox_stream, {"data": json.dumps(event.raw)})
            logger.info(
                "[Router] routed to stream=%r status=%s", inbox_stream, event.status
            )
            return True

        # distr-pull fallback: no local registry entry.  The worker is a
        # subprocess spawned by a daemon on some machine.  Check the
        # task:owner:{bug_id} key that the daemon sets on spawn.
        owner_key = f"task:owner:{event.bug_id}"
        owner = await self._redis.get(owner_key)
        if owner:
            inbox_stream = self._inbox_stream_tpl.format(bug_id=event.bug_id)
            await self._redis.xadd(inbox_stream, {"data": json.dumps(event.raw)})
            logger.info(
                "[Router] routed to stream=%r (distr-pull, owner=%s) status=%s",
                inbox_stream,
                owner.decode() if isinstance(owner, bytes) else owner,
                event.status,
            )
            return True

        logger.warning("[Router] no active worker for bug_id=%s", event.bug_id)
        return False
