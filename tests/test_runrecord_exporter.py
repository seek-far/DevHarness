"""tools/runrecord_exporter.py — HTTP exposition tests.

These pin the K8s-shaped scrape contract:
  * GET /metrics returns 200 + Prometheus text format
  * Output is functionally equivalent to what runrecord_to_metrics.py
    would have written (same _scan_journal + _aggregate + render path)
  * Empty journal still returns a valid scrape (counters at 0)
  * Non-/metrics paths 404
  * A poisoned record.json returns 500 (visible as `up{}=0` to
    Prometheus) rather than serving a half-rendered body

Stdlib-only: ThreadingHTTPServer + urllib. Each test boots its own
server on an ephemeral port so they can run in parallel.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.runrecord_exporter import serve


def _write_record(parent: Path, name: str, record: dict) -> None:
    d = parent / name
    d.mkdir()
    (d / "record.json").write_text(json.dumps(record), encoding="utf-8")


def _make_record(**overrides) -> dict:
    base = {
        "agent_name": "langgraph",
        "llm_model": "qwen3-coder-480b-a35b-instruct",
        "outcome": "fixed",
        "elapsed_s": 60.0,
        "total_prompt_tokens": 1000,
        "total_completion_tokens": 100,
        "total_cached_input_tokens": None,
        "llm_call_count": 3,
        "parse_trace_fallback": False,
        "reflection_count": None,
    }
    base.update(overrides)
    return base


@pytest.fixture
def server_at(tmp_path):
    """Spin up a server on an ephemeral port; tear down on exit."""
    servers = []

    def _start(journal_dir: Path):
        # port=0 → kernel picks a free port. server.server_address[1] is
        # the actual port post-bind.
        server = serve(journal_dir, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return server.server_address[1]

    yield _start

    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _get(port: int, path: str = "/metrics"):
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}{path}", timeout=5,
    ) as resp:
        return resp.status, resp.read().decode("utf-8")


def test_metrics_endpoint_returns_prometheus_text(server_at, tmp_path):
    """Smoke: one record in, one fixes_completed sample out, valid
    Prometheus exposition format. Pins the wire contract Prometheus
    scrapes from."""
    journal = tmp_path / "journal"
    journal.mkdir()
    _write_record(journal, "20260530T120000_BUG-A_langgraph", _make_record())

    port = server_at(journal)
    # Give the bind a beat on slower CI runners.
    time.sleep(0.05)
    status, body = _get(port)

    assert status == 200
    # Same shape runrecord_to_metrics.render produces — sanity-check a
    # couple of representative lines without re-asserting the whole
    # contract (test_runrecord_to_metrics covers that exhaustively).
    assert "# HELP sdlcma_fixes_completed_total " in body
    assert "# TYPE sdlcma_fixes_completed_total counter" in body
    assert (
        'sdlcma_fixes_completed_total{agent="langgraph",'
        'model_slug="qwen3-coder-480b-a35b-instruct",outcome="fixed"} 1'
    ) in body
    assert "sdlcma_runrecord_last_scan_timestamp " in body


def test_metrics_endpoint_with_empty_journal_still_200(server_at, tmp_path):
    """Brand-new install — no fixes yet. Prometheus should NOT see this
    as a scrape failure; we serve a 200 with the counter families at 0
    so the time series stays continuous."""
    empty = tmp_path / "empty_journal"
    # Don't even create the directory — exporter must handle it.
    port = server_at(empty)
    time.sleep(0.05)
    status, body = _get(port)
    assert status == 200
    # Families still rendered (HELP + TYPE lines present), counters at 0.
    assert "# HELP sdlcma_fixes_completed_total " in body
    assert "# TYPE sdlcma_fixes_completed_total counter" in body


def test_non_metrics_path_returns_404(server_at, tmp_path):
    """Misconfigured Prometheus scrape (path=/healthz, /, ...) must NOT
    return a successful empty body — that would silently pollute the
    scrape with no metrics. 404 makes the misconfig visible immediately."""
    journal = tmp_path / "journal"
    journal.mkdir()
    port = server_at(journal)
    time.sleep(0.05)

    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(port, "/")
    assert exc.value.code == 404


def test_handles_poisoned_record_with_500(server_at, tmp_path, monkeypatch):
    """A record.json that's syntactically valid but trips the renderer
    must return 500 to Prometheus (visible as `up=0` in the dashboard's
    Health row) rather than a half-rendered body. We monkeypatch
    _aggregate to raise — simpler than crafting a record whose data
    happens to crash render."""
    import tools.runrecord_exporter as exporter

    def _boom(_records):
        raise RuntimeError("simulated render crash")

    monkeypatch.setattr(exporter, "_aggregate", _boom)

    journal = tmp_path / "journal"
    journal.mkdir()
    _write_record(journal, "20260530T120000_BUG-B_langgraph", _make_record())

    port = server_at(journal)
    time.sleep(0.05)

    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(port)
    assert exc.value.code == 500


def test_each_scrape_is_a_fresh_aggregation(server_at, tmp_path):
    """Critical for the long-running model: a new record dropped into
    the journal between scrapes must show up in the very next /metrics
    response, not be cached from the first scan. Same freshness story
    as the textfile path's cron cadence, but bounded by scrape_interval
    instead of cron interval."""
    journal = tmp_path / "journal"
    journal.mkdir()
    _write_record(journal, "20260530T120000_BUG-1_langgraph", _make_record())

    port = server_at(journal)
    time.sleep(0.05)
    _, body1 = _get(port)
    assert (
        'sdlcma_fixes_completed_total{agent="langgraph",'
        'model_slug="qwen3-coder-480b-a35b-instruct",outcome="fixed"} 1'
    ) in body1

    _write_record(journal, "20260530T120100_BUG-2_langgraph", _make_record())
    _, body2 = _get(port)
    assert (
        'sdlcma_fixes_completed_total{agent="langgraph",'
        'model_slug="qwen3-coder-480b-a35b-instruct",outcome="fixed"} 2'
    ) in body2
