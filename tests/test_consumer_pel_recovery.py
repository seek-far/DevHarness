"""StreamConsumer PEL recovery at startup (Item 6.2).

The orchestrator's StreamConsumer reads `gateway:stream` via a Redis
consumer group. When the previous orchestrator process died between
XREADGROUP and XACK on a message, that entry sits in the consumer's
Pending Entry List forever — the main `>` loop only delivers NEW
entries, so the old webhook is never processed and the bug it describes
is permanently lost.

The fix: at consumer startup, before the main `>` loop, drain this
consumer's PEL with XREADGROUP id="0". Each PEL entry runs through the
same handler + ACK path as a normal message; the dead-letter route
catches handler errors so trimmed/empty entries don't loop forever.

These tests pin:
  * Startup drains PEL entries before entering the main loop.
  * Empty PEL → drain returns immediately, no spurious handler calls.
  * Drain ACKs each entry processed (otherwise XREADGROUP "0" returns
    the same entries on the next pass).
  * Handler exceptions on a PEL entry route to dead-letter + ACK
    (existing _process_entry contract — verified end-to-end here).
  * Max-passes cap prevents a pathological group from blocking
    startup forever.
  * A Redis error during drain logs a warning but doesn't crash —
    the main `>` loop has its own retry.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.consumer import StreamConsumer


def _consumer(redis, handler, *, count: int = 10) -> StreamConsumer:
    return StreamConsumer(
        redis=redis,
        stream_key="gateway:stream",
        group="orchestrator-group",
        consumer_name="consumer-1",
        handler=handler,
        dead_letter_stream="orchestrator:dead_letter",
        block_ms=1000,
        count=count,
    )


def _xreadgroup_result(entries: list[tuple[bytes, dict]]):
    """Shape redis-py xreadgroup returns: list of (stream_key, entries)."""
    return [(b"gateway:stream", entries)] if entries else []


# ── basic drain flow ────────────────────────────────────────────────────────


def test_drain_processes_pending_entries_in_order():
    """Headline behaviour: PEL with two entries → handler called twice
    in delivery order, each ACKed, then drain returns."""
    redis = MagicMock()
    redis.xreadgroup = AsyncMock(side_effect=[
        _xreadgroup_result([
            (b"100-0", {b"data": b'{"id": 1}'}),
            (b"200-0", {b"data": b'{"id": 2}'}),
        ]),
        _xreadgroup_result([]),  # second pass: PEL empty
    ])
    redis.xack = AsyncMock()
    redis.xadd = AsyncMock()

    handler_calls: list[bytes] = []

    async def handler(raw: bytes):
        handler_calls.append(raw)

    c = _consumer(redis, handler)
    asyncio.run(c._drain_pending())

    assert handler_calls == [b'{"id": 1}', b'{"id": 2}']
    assert redis.xack.call_count == 2
    # First call read with id="0" (the PEL marker, not ">").
    first_call_kwargs = redis.xreadgroup.call_args_list[0].kwargs
    assert first_call_kwargs["streams"] == {"gateway:stream": "0"}


def test_drain_returns_immediately_on_empty_pel():
    """A fresh orchestrator with no prior state must not pay any
    handler cost — drain should make one xreadgroup call, see empty,
    return."""
    redis = MagicMock()
    redis.xreadgroup = AsyncMock(return_value=_xreadgroup_result([]))
    redis.xack = AsyncMock()
    handler = AsyncMock()

    c = _consumer(redis, handler)
    asyncio.run(c._drain_pending())

    assert handler.await_count == 0
    assert redis.xack.await_count == 0
    assert redis.xreadgroup.call_count == 1


def test_drain_handles_trimmed_entry_via_dead_letter():
    """A PEL entry whose stream data was trimmed away returns with
    empty fields. The handler then raises on the empty data; the
    existing _process_entry contract routes it to dead-letter and
    ACKs so it doesn't loop forever."""
    redis = MagicMock()
    redis.xreadgroup = AsyncMock(side_effect=[
        # Empty fields = trimmed entry.
        _xreadgroup_result([(b"999-0", {})]),
        _xreadgroup_result([]),
    ])
    redis.xack = AsyncMock()
    redis.xadd = AsyncMock()

    async def handler(raw: bytes):
        # Realistic parse_message would raise on empty input.
        raise ValueError("empty payload")

    c = _consumer(redis, handler)
    asyncio.run(c._drain_pending())

    # Dead-letter written + entry ACKed so it never comes back.
    redis.xadd.assert_awaited_once()
    redis.xack.assert_awaited_once_with(
        "gateway:stream", "orchestrator-group", b"999-0")


def test_drain_loops_until_empty():
    """When count=2 and PEL has 5 entries, drain must keep reading
    until empty rather than stopping after the first batch."""
    redis = MagicMock()
    # 3 batches: 2 entries, 2 entries, 1 entry, then empty.
    redis.xreadgroup = AsyncMock(side_effect=[
        _xreadgroup_result([(b"1-0", {b"data": b"a"}),
                            (b"2-0", {b"data": b"b"})]),
        _xreadgroup_result([(b"3-0", {b"data": b"c"}),
                            (b"4-0", {b"data": b"d"})]),
        _xreadgroup_result([(b"5-0", {b"data": b"e"})]),
        _xreadgroup_result([]),
    ])
    redis.xack = AsyncMock()
    handler = AsyncMock()

    c = _consumer(redis, handler, count=2)
    asyncio.run(c._drain_pending())

    assert handler.await_count == 5
    assert redis.xack.await_count == 5


def test_drain_bounded_by_max_passes(monkeypatch):
    """If something pathological keeps returning the same entries
    (handler crashes BEFORE ACK, every pass), drain caps at 100
    iterations to keep startup from deadlocking. Real recovery still
    happens via the main `>` loop afterwards."""
    redis = MagicMock()
    # Always returns one entry — handler will crash + dead-letter +
    # ACK normally, but if ACK itself is broken the loop is bounded.
    redis.xreadgroup = AsyncMock(return_value=_xreadgroup_result(
        [(b"7-0", {b"data": b"x"})]
    ))
    # Make xack a no-op so the entry STAYS in PEL → simulates the
    # pathological "ACK silently dropped" case.
    redis.xack = AsyncMock()
    redis.xadd = AsyncMock()

    async def handler(raw: bytes):
        # Successful handle so we never hit dead-letter path.
        return

    c = _consumer(redis, handler)
    asyncio.run(c._drain_pending())
    # Bound: max_passes=100 in code. Each pass calls xreadgroup once.
    assert redis.xreadgroup.call_count == 100


def test_drain_xreadgroup_error_logs_and_returns(caplog):
    """A redis-side error during drain (e.g. group temporarily
    unavailable) shouldn't crash startup. Log a warning, fall through
    to the main `>` loop which has its own retry."""
    import logging
    caplog.set_level(logging.WARNING, logger="orchestrator.consumer")
    redis = MagicMock()
    redis.xreadgroup = AsyncMock(side_effect=RuntimeError("redis temporary blip"))
    handler = AsyncMock()

    c = _consumer(redis, handler)
    asyncio.run(c._drain_pending())
    assert any("PEL drain failed" in r.message for r in caplog.records)
    assert handler.await_count == 0
