"""
StreamConsumer: uses Redis Streams + Consumer Group to replace the original BLPOP List consumption pattern.

Core flow:
  1. On startup, ensure the stream / consumer group exists (XGROUP CREATE … MKSTREAM)
  2. XREADGROUP blocks reading new messages (> means only entries not yet delivered)
  3. XACK on successful processing; write to dead-letter stream on failure
  4. Each loop also handles timed-out unconfirmed messages in PEL (Pending Entry List) (optional)
"""

import asyncio
import logging
from typing import Awaitable, Callable

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

MessageHandler = Callable[[bytes], Awaitable[None]]


class StreamConsumer:
    def __init__(
        self,
        redis: Redis,
        stream_key: str,
        group: str,
        consumer_name: str,
        handler: MessageHandler,
        dead_letter_stream: str,
        block_ms: int = 2000,
        count: int = 10,
    ):
        self._redis = redis
        self._stream_key = stream_key
        self._group = group
        self._consumer_name = consumer_name
        self._handler = handler
        self._dead_letter_stream = dead_letter_stream
        self._block_ms = block_ms
        self._count = count
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.get_event_loop().create_task(
            self._run(), name="StreamConsumer"
        )
        logger.info(
            "[Consumer] started stream=%r group=%r consumer=%r",
            self._stream_key, self._group, self._consumer_name,
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[Consumer] stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _ensure_group(self) -> None:
        """Create consumer group, ignore if it already exists. MKSTREAM ensures the stream itself exists."""
        try:
            await self._redis.xgroup_create(
                self._stream_key, self._group, id="0", mkstream=True
            )
            logger.info(
                "[Consumer] created group=%r on stream=%r",
                self._group, self._stream_key,
            )
        except Exception as e:
            # BUSYGROUP: group already exists — normal case, ignore
            if "BUSYGROUP" in str(e):
                logger.debug("[Consumer] group already exists, skipping create")
            else:
                raise

    async def _run(self) -> None:
        await self._ensure_group()
        while True:
            try:
                # Read new messages (">" means only entries not yet delivered to any consumer)
                results = await self._redis.xreadgroup(
                    groupname=self._group,
                    consumername=self._consumer_name,
                    streams={self._stream_key: ">"},
                    count=self._count,
                    block=self._block_ms,
                )
                if not results:
                    continue  # timed out, continue loop

                # results: [ (stream_key, [ (entry_id, {field: value, ...}), ... ]) ]
                for _stream, entries in results:
                    for entry_id, fields in entries:
                        await self._process_entry(entry_id, fields)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[Consumer] Redis error: %s, retrying in 1s", e)
                await asyncio.sleep(1)

    async def _process_entry(self, entry_id: bytes, fields: dict) -> None:
        """Process a single stream entry: XACK on success, write to dead-letter on failure."""
        # Message body is stored uniformly in the "data" field
        raw: bytes = fields.get(b"data") or fields.get("data", b"")
        try:
            await self._handler(raw)
            await self._redis.xack(self._stream_key, self._group, entry_id)
            logger.debug("[Consumer] ack entry_id=%s", entry_id)
        except Exception as e:
            logger.exception(
                "[Consumer] handler error: %r, dead-lettering entry_id=%s", e, entry_id
            )
            try:
                # Write to dead-letter stream, preserving original data and error info
                await self._redis.xadd(
                    self._dead_letter_stream,
                    {
                        "data": raw,
                        "error": str(e),
                        "origin_stream": self._stream_key,
                        "origin_id": entry_id,
                    },
                )
                # Still ack to avoid infinite retry of the same bad message
                await self._redis.xack(self._stream_key, self._group, entry_id)
            except Exception as re:
                logger.error("[Consumer] dead-letter push failed: %s", re)
