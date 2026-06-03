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

from .metrics import DEAD_LETTER

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
        # run() never awaits this task (it blocks on a shutdown signal), so a
        # task that dies with an exception would otherwise be silent: the
        # exception is stored on the Task and never retrieved (no traceback),
        # the pod stays 1/1, and consumption simply stops. This callback makes
        # any unexpected exit loud. _run() is itself self-healing (see below),
        # so in practice this should only ever fire on a clean cancel.
        self._task.add_done_callback(self._on_task_done)
        logger.info(
            "[Consumer] started stream=%r group=%r consumer=%r",
            self._stream_key, self._group, self._consumer_name,
        )

    @staticmethod
    def _on_task_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.critical(
                "[Consumer] consume task EXITED unexpectedly (%r) — stream "
                "consumption has STOPPED; orchestrator restart required",
                exc,
                exc_info=exc,
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
        """Supervisory wrapper. _ensure_group() and _drain_pending() run
        BEFORE the read loop and are NOT covered by its try/except. A
        transient redis error here at startup — e.g. a full-host reboot
        where redis isn't ready yet and xgroup_create raises ConnectionError
        or "LOADING" (anything that isn't BUSYGROUP) — used to escape _run()
        and kill the consume task silently (see start()). Wrapping the whole
        sequence in the same retry as the loop makes the task self-heal: it
        keeps retrying ensure_group/drain until redis comes up, then settles
        into the read loop. The only clean exit is CancelledError (stop())."""
        while True:
            try:
                await self._ensure_group()
                # One-shot drain of any PEL entries delivered to this consumer
                # name but never ACKed — i.e. messages that were in flight when
                # the previous orchestrator process died. Without this, a
                # webhook that XREADGROUP'd into the orchestrator's PEL but
                # never got handled (orchestrator crash between read and ACK)
                # sits there forever, blocked by the consumer group's
                # last-delivered-id pointer. XREADGROUP id="0" returns ALL
                # entries currently in this consumer's PEL; we process + ACK
                # each, and the loop ends when there are no more. Trimmed
                # entries (Redis-trimmed away while in PEL) arrive with empty
                # fields; _process_entry's handler will then raise on the empty
                # `data`, route to dead_letter, and ACK so they stop reappearing.
                await self._drain_pending()
                await self._consume_loop()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "[Consumer] startup/consume sequence crashed: %s; "
                    "retrying in 1s", e,
                )
                await asyncio.sleep(1)

    async def _consume_loop(self) -> None:
        """Main read loop: blocking XREADGROUP on new (">") entries."""
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

    async def _drain_pending(self) -> None:
        """Process this consumer's PEL once at startup. Each pass reads
        up to `count` entries with id="0"; ACKing entries removes them
        from PEL, so the next pass sees the rest. Loop terminates when
        XREADGROUP returns no entries.

        Bounded by a hard iteration cap so a pathological PEL (huge AND
        every entry handler errors AND every dead-letter write fails)
        can't deadlock startup."""
        max_passes = 100
        for _ in range(max_passes):
            try:
                results = await self._redis.xreadgroup(
                    groupname=self._group,
                    consumername=self._consumer_name,
                    streams={self._stream_key: "0"},
                    count=self._count,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Group/stream lookup failed — don't block startup on it;
                # the main `>` loop has its own retry. Log loudly because
                # this means PEL recovery is silently skipped.
                logger.warning("[Consumer] PEL drain failed: %s "
                               "(continuing to main loop)", e)
                return
            entries = []
            for _stream, batch in (results or []):
                entries.extend(batch)
            if not entries:
                return
            logger.info("[Consumer] PEL drain: processing %d pending entry(ies) "
                        "from prior process", len(entries))
            for entry_id, fields in entries:
                await self._process_entry(entry_id, fields)
        logger.warning("[Consumer] PEL drain hit max_passes=%d; "
                       "remaining entries will be handled by the main loop",
                       max_passes)

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
                DEAD_LETTER.labels(stream=self._stream_key).inc()
                # Still ack to avoid infinite retry of the same bad message
                await self._redis.xack(self._stream_key, self._group, entry_id)
            except Exception as re:
                logger.error("[Consumer] dead-letter push failed: %s", re)
