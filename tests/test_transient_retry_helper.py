"""Tests for the shared transient-retry helper.

The classifier itself is covered in detail by tests/test_fetch_trace_retry.py
(which exercises the same `classify_transient` / `parse_retry_after` symbols).
This file adds the cases that are unique to the shared helper:

  - The Redis branch (deferred-imported, exercised here so it isn't dead in
    coverage on machines that have redis-py installed).
  - The `with_transient_retry` return-shape contract (returns
    `(result, retries)`; raises on permanent / exhausted; sleeps come from the
    helper module so monkey-patching by path works for downstream node tests).
"""

from __future__ import annotations

import errno
import sys
from pathlib import Path

import pytest
import redis.exceptions as redis_exc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from services.transient_retry import (  # noqa: E402
    DEFAULT_RETRY_DELAYS,
    classify_transient,
    with_transient_retry,
)


# ── Redis classifier ─────────────────────────────────────────────────────────


def test_redis_connection_error_transient():
    transient, delay = classify_transient(redis_exc.ConnectionError("disc"))
    assert transient is True
    assert delay is None


def test_redis_timeout_error_transient():
    assert classify_transient(redis_exc.TimeoutError("slow"))[0] is True


def test_redis_busy_loading_transient():
    # Surfaces while Redis is loading data on startup — short-lived.
    assert classify_transient(redis_exc.BusyLoadingError("loading"))[0] is True


def test_redis_response_error_permanent():
    # A protocol-level / command error from Redis is not a transport flake;
    # propagate so the operator sees the underlying issue.
    assert classify_transient(redis_exc.ResponseError("WRONGTYPE"))[0] is False


# ── with_transient_retry contract ────────────────────────────────────────────


def test_with_transient_retry_first_attempt(monkeypatch):
    monkeypatch.setattr("services.transient_retry.time.sleep", lambda s: None)
    result, retries = with_transient_retry(lambda: "ok", op_name="t")
    assert result == "ok"
    assert retries == 0


def test_with_transient_retry_one_transient_then_success(monkeypatch):
    monkeypatch.setattr("services.transient_retry.time.sleep", lambda s: None)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EAGAIN, "again")
        return "fine"

    result, retries = with_transient_retry(fn, op_name="t")
    assert result == "fine"
    assert retries == 1
    assert calls["n"] == 2


def test_with_transient_retry_exhausts(monkeypatch):
    monkeypatch.setattr("services.transient_retry.time.sleep", lambda s: None)

    def fn():
        raise OSError(errno.EAGAIN, "again")

    with pytest.raises(OSError):
        with_transient_retry(fn, op_name="t")


def test_with_transient_retry_propagates_permanent_immediately(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(
        "services.transient_retry.time.sleep", lambda s: sleeps.append(s)
    )
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise FileNotFoundError("/no/such")

    with pytest.raises(FileNotFoundError):
        with_transient_retry(fn, op_name="t")

    assert calls["n"] == 1
    assert sleeps == []  # no retry attempted


def test_with_transient_retry_custom_delays_used(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(
        "services.transient_retry.time.sleep", lambda s: sleeps.append(s)
    )

    raised = [True, True, False]

    def fn():
        if raised.pop(0):
            raise OSError(errno.EBUSY, "busy")
        return "ok"

    result, retries = with_transient_retry(fn, op_name="t", delays=(5, 7))
    assert result == "ok"
    assert retries == 2
    assert sleeps == [5, 7]


def test_default_retry_delays_export():
    # Other tests rely on this symbol; guard against accidental rename.
    assert isinstance(DEFAULT_RETRY_DELAYS, tuple)
    assert all(isinstance(d, int) and d > 0 for d in DEFAULT_RETRY_DELAYS)
