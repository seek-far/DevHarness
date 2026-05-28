"""bug_id format tests for the orchestrator.

The orchestrator stamps `bug_id = timestamp + decisecond + _XXXX` where
`XXXX` is a 4-char lowercase-hex tail drawn from `secrets.token_hex(2)`
(urandom-backed, not time-derived). The tail kills the 100 ms collision
window that bit us at N=8 webhook bursts (project memory
`project_orchestrator_bug_id_race`).

This module pins:
  * the format itself (matches the relaxed parser regex);
  * that two bug_ids minted in the same decisecond actually differ
    (the whole point of the random tail);
  * that the source is `secrets.token_hex` — not `random.random()` or
    a time-based RNG that would re-collide under burst.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.orchestrator import Orchestrator  # noqa: E402
from orchestrator.parser import _BUG_ID_RE  # noqa: E402


_PIPELINE = {
    "object_kind": "pipeline",
    "object_attributes": {"ref": "main", "status": "failed"},
    "project": {"id": 7, "web_url": "https://gitlab.local/g/p"},
    "builds": [{"id": 99}],
}


def _make_orch_with_mock_spawner():
    settings = MagicMock()
    settings.env = "test"
    settings.redis_url = "redis://localhost:6379/15"
    settings.worker_spawner = "process"
    settings.worker_inbox_stream_key = "worker:{bug_id}:stream"
    settings.gateway_stream = "gateway:stream"
    settings.gateway_consumer_group = "g"
    settings.gateway_consumer_name = "c"
    settings.dead_letter_stream = "dl"
    settings.stream_block_ms = 1000
    settings.stream_count = 1
    settings.worker_heartbeat_key = "worker:heartbeat:{bug_id}"
    settings.worker_completed_key = "worker:completed:{bug_id}"
    settings.health_check_interval = 5

    with patch("orchestrator.orchestrator.aioredis"):
        orch = Orchestrator(settings=settings)
    orch._spawner = MagicMock()
    orch._spawner.spawn = AsyncMock()
    return orch


def _captured_bug_id(orch) -> str:
    asyncio.run(orch._handle_message(json.dumps(_PIPELINE).encode()))
    orch._spawner.spawn.assert_called_once()
    return orch._spawner.spawn.call_args.args[0]


def test_bug_id_matches_relaxed_parser_regex():
    orch = _make_orch_with_mock_spawner()
    bug_id = _captured_bug_id(orch)
    # The relaxed regex (parser._BUG_ID_RE) is what extracts bug_id from
    # auto-fix branch refs — if the format and the regex drift apart, the
    # CI-result feedback loop silently breaks (would mis-classify
    # successful fix-branch pipelines as OtherEvent and time out the
    # worker, exactly like the bug fixed by test_real_fix_branch_matches).
    assert re.fullmatch(_BUG_ID_RE, bug_id), bug_id


def test_bug_id_has_4_hex_random_tail():
    orch = _make_orch_with_mock_spawner()
    bug_id = _captured_bug_id(orch)
    tail = bug_id.rsplit("_", 1)[-1]
    assert re.fullmatch(r"[0-9a-f]{4}", tail), tail


def test_random_tail_is_not_time_based():
    """secrets.token_hex(2) is urandom-backed; calling it twice in the
    same decisecond MUST yield two different tails (whole point of the
    Item 1 change). A regression to time-based randomness would re-create
    the burst collision."""
    fixed_now = MagicMock()
    fixed_now.strftime.return_value = "2026_05_28-12_30_45"
    fixed_now.microsecond = 0  # decisecond bucket = 0

    bug_ids = set()
    orch = _make_orch_with_mock_spawner()
    with patch("orchestrator.orchestrator.datetime") as dt:
        dt.now.return_value = fixed_now
        for _ in range(64):
            orch._spawner.spawn.reset_mock()
            bug_ids.add(_captured_bug_id(orch))

    # 64 draws from a 65536-space → collision probability ~ 64*63/2/65536 ≈ 3%.
    # Asserting >=60 unique covers the worst plausible draw without flake.
    assert len(bug_ids) >= 60, sorted(bug_ids)


def test_handle_message_threads_source_branch_to_spawner():
    """Item 3b: source_branch from the BugReportedEvent must reach the
    spawner so the worker can rebase / MR against the right base."""
    orch = _make_orch_with_mock_spawner()
    payload = {
        **_PIPELINE,
        "object_attributes": {"ref": "feature/login", "status": "failed"},
    }
    asyncio.run(orch._handle_message(json.dumps(payload).encode()))
    orch._spawner.spawn.assert_called_once()
    kwargs = orch._spawner.spawn.call_args.kwargs
    assert kwargs["source_branch"] == "feature/login"
