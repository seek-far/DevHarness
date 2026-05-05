"""Wiring tests for the transient-retry layer applied to the four I/O nodes:
fetch_source_file, commit_change, wait_ci_result, create_mr.

Each test exercises one node end-to-end against a stub provider whose method
raises a queue of transient exceptions before finally succeeding (or failing
permanently). We assert:

  - The retry counter (`<node>_retries`) lands in the returned state when
    the call succeeds.
  - Permanent errors propagate (or fall through to the existing fallback,
    in fetch_source_file's case) without retrying.
  - The shared sleep entry-point is monkey-patched away so the suite stays
    fast.

The classifier itself + Retry-After parsing are covered in
test_fetch_trace_retry.py and test_transient_retry_helper.py.
"""

from __future__ import annotations

import errno
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from graph.nodes.commit_change import commit_change  # noqa: E402
from graph.nodes.create_mr import create_mr  # noqa: E402
from graph.nodes.fetch_source_file import fetch_source_file  # noqa: E402
from graph.nodes.wait_ci_result import wait_ci_result  # noqa: E402
from services.transient_retry import DEFAULT_RETRY_DELAYS  # noqa: E402


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Never actually sleep during retry tests."""
    monkeypatch.setattr("services.transient_retry.time.sleep", lambda s: None)


def _make_queue(exceptions, success_value):
    """Return a callable that raises each queued exception once, then returns
    success_value on every subsequent call."""
    queue = list(exceptions)

    def _call(*args, **kwargs):
        if queue:
            raise queue.pop(0)
        return success_value

    return _call


# ── fetch_source_file ────────────────────────────────────────────────────────


class _FetchFileProvider:
    def __init__(self, exceptions, content="def f(): return 1\n"):
        self.fetch_file = _make_queue(exceptions, content)


def test_fetch_source_file_one_transient_then_success():
    provider = _FetchFileProvider(
        [requests.exceptions.ConnectionError("blip")],
        content="def x(): return 42\n",
    )
    out = fetch_source_file({"provider": provider, "suspect_file_path": "x.py"})
    assert out["source_fetch_failed"] is False
    assert out["source_file_content"] == "def x(): return 42\n"
    assert out["fetch_source_file_retries"] == 1


def test_fetch_source_file_falls_back_after_exhausted_transients():
    # Enough transients to exhaust the budget; node must still return
    # (not raise), with source_fetch_failed=True so the LLM can recover.
    n = 1 + len(DEFAULT_RETRY_DELAYS)
    provider = _FetchFileProvider(
        [requests.exceptions.Timeout("t")] * n,
    )
    out = fetch_source_file({"provider": provider, "suspect_file_path": "y.py"})
    assert out["source_fetch_failed"] is True
    assert out["source_file_content"] == ""
    # Best-effort count: when the failure is transient and the budget is
    # exhausted, all retry slots fired.
    assert out["fetch_source_file_retries"] == len(DEFAULT_RETRY_DELAYS)


def test_fetch_source_file_permanent_falls_back_with_zero_retries():
    # FileNotFoundError is permanent — no retries happen, but the node still
    # falls through to source_fetch_failed mode (this is the path that lets
    # the LLM cope with a parser hint that points at a moved file).
    provider = _FetchFileProvider([FileNotFoundError("/no/such")])
    out = fetch_source_file({"provider": provider, "suspect_file_path": "z.py"})
    assert out["source_fetch_failed"] is True
    assert out["fetch_source_file_retries"] == 0


# ── commit_change ────────────────────────────────────────────────────────────


class _CommitProvider:
    def __init__(self, exceptions, repo_path=Path("/tmp/repo")):
        self._repo_path = repo_path
        self.commit_and_push = _make_queue(
            exceptions,
            {"status": "success", "branch": "auto/x", "commit": "deadbeef"},
        )

    def ensure_repo_ready(self, bug_id):
        return self._repo_path


def test_commit_change_one_transient_then_success():
    provider = _CommitProvider([requests.exceptions.ConnectionError("blip")])
    out = commit_change({"provider": provider, "bug_id": "B"})
    assert out["commit_status"] == "success"
    assert out["commit_hash"] == "deadbeef"
    assert out["commit_change_retries"] == 1


def test_commit_change_zero_retries_on_clean_call():
    provider = _CommitProvider([])
    out = commit_change({"provider": provider, "bug_id": "B"})
    assert out["commit_change_retries"] == 0


def test_commit_change_permanent_propagates():
    err = requests.exceptions.HTTPError("403")
    err.response = requests.Response()
    err.response.status_code = 403
    provider = _CommitProvider([err])
    with pytest.raises(requests.exceptions.HTTPError):
        commit_change({"provider": provider, "bug_id": "B"})


def test_commit_change_exhausted_transients_propagate():
    n = 1 + len(DEFAULT_RETRY_DELAYS)
    provider = _CommitProvider(
        [requests.exceptions.Timeout("t")] * n,
    )
    with pytest.raises(requests.exceptions.Timeout):
        commit_change({"provider": provider, "bug_id": "B"})


# ── wait_ci_result ───────────────────────────────────────────────────────────


class _CIProvider:
    def __init__(self, exceptions, status="success"):
        self.wait_ci_result = _make_queue(exceptions, status)


def test_wait_ci_result_one_transient_then_success():
    import redis.exceptions as redis_exc
    provider = _CIProvider([redis_exc.ConnectionError("disc")])
    out = wait_ci_result({"provider": provider, "bug_id": "B"})
    assert out["ci_status"] == "success"
    assert out["wait_ci_result_retries"] == 1


def test_wait_ci_result_timeout_is_not_retried():
    # `None` from the provider means "I waited the full timeout; nothing
    # arrived" — that's a real result, not a transient. Must be passed
    # through with retries=0, no sleep, no extra call.
    provider = _CIProvider([], status=None)
    out = wait_ci_result({"provider": provider, "bug_id": "B"})
    assert out["ci_status"] == "timeout"
    assert out["wait_ci_result_retries"] == 0


def test_wait_ci_result_permanent_propagates():
    import redis.exceptions as redis_exc
    provider = _CIProvider([redis_exc.ResponseError("WRONGTYPE")])
    with pytest.raises(redis_exc.ResponseError):
        wait_ci_result({"provider": provider, "bug_id": "B"})


# ── create_mr ────────────────────────────────────────────────────────────────


class _ReviewProvider:
    def __init__(self, exceptions, repo_path=Path("/tmp/repo")):
        self._repo_path = repo_path
        self.create_review = _make_queue(
            exceptions,
            {"id": 116, "iid": 20, "url": "http://gitlab.local/x/-/mr/20",
             "state": "opened", "branch": "auto/x"},
        )

    def ensure_repo_ready(self, bug_id):
        return self._repo_path


def test_create_mr_one_transient_then_success():
    err = requests.exceptions.HTTPError("503")
    err.response = requests.Response()
    err.response.status_code = 503
    provider = _ReviewProvider([err])
    out = create_mr({"provider": provider, "bug_id": "B"})
    assert out["review_status"] == "opened"
    assert out["review_url"].endswith("/mr/20")
    assert out["create_mr_retries"] == 1


def test_create_mr_permanent_propagates():
    err = requests.exceptions.HTTPError("404")
    err.response = requests.Response()
    err.response.status_code = 404
    provider = _ReviewProvider([err])
    with pytest.raises(requests.exceptions.HTTPError):
        create_mr({"provider": provider, "bug_id": "B"})


def test_create_mr_zero_retries_on_clean_call():
    provider = _ReviewProvider([])
    out = create_mr({"provider": provider, "bug_id": "B"})
    assert out["create_mr_retries"] == 0
