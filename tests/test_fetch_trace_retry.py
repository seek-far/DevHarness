"""Tests for the fetch_trace transient-retry layer.

Behavior under test:
  1. Transient HTTP exceptions (ConnectionError, Timeout, ChunkedEncodingError)
     trigger a retry; success on attempt N reports retries=N-1.
  2. HTTP 5xx and 429 are transient. 429 honors Retry-After when present.
  3. HTTP 4xx other than 429 is permanent — propagates immediately, no retry.
  4. OSError with a transient errno (EAGAIN, EBUSY, EIO, ETIMEDOUT, …) is
     retried; FileNotFoundError / PermissionError are not.
  5. Retry budget exhaustion: after the configured number of retries the
     last transient exception propagates to the caller.
  6. The fetch_trace_retries counter lands in the returned state for
     downstream telemetry / RunRecord wiring.
  7. _parse_retry_after handles seconds, None / empty / non-numeric, negative,
     and oversize values (clamped at _RETRY_AFTER_CAP_S).
"""

from __future__ import annotations

import errno
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from graph.nodes.fetch_trace import (  # noqa: E402
    _RETRY_AFTER_CAP_S,
    _RETRY_DELAYS,
    _classify_transient,
    _parse_retry_after,
    fetch_trace,
)


# ── helpers ──────────────────────────────────────────────────────────────────


class _Provider:
    """Provider stub whose fetch_trace raises a queue of exceptions before
    finally returning a trace string. Lets us simulate "first N attempts fail
    transiently, then succeed"."""

    def __init__(self, exceptions, success_value="trace text"):
        self._exceptions = list(exceptions)
        self._success = success_value
        self.call_count = 0

    def fetch_trace(self, **kwargs):
        self.call_count += 1
        if self._exceptions:
            raise self._exceptions.pop(0)
        return self._success


def _http_error(status_code: int, retry_after=None):
    """Build a requests.HTTPError with a synthetic response carrying status
    and (optionally) a Retry-After header."""
    resp = requests.Response()
    resp.status_code = status_code
    if retry_after is not None:
        resp.headers["Retry-After"] = str(retry_after)
    err = requests.exceptions.HTTPError(f"HTTP {status_code}")
    err.response = resp
    return err


def _state(provider):
    return {
        "provider": provider,
        "project_id": "p",
        "job_id": "j",
    }


# ── _parse_retry_after unit ──────────────────────────────────────────────────


def test_parse_retry_after_seconds():
    assert _parse_retry_after("5") == 5.0
    assert _parse_retry_after("  10  ") == 10.0


def test_parse_retry_after_clamps_oversize():
    # Server can hand back arbitrarily large values; we cap so a single
    # request can't block the whole run past its wallclock budget.
    assert _parse_retry_after("3600") == float(_RETRY_AFTER_CAP_S)


def test_parse_retry_after_rejects_invalid():
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after("not a number") is None
    # Negative values are nonsensical — fall back to default backoff.
    assert _parse_retry_after("-5") is None
    assert _parse_retry_after("0") is None


def test_parse_retry_after_rejects_http_date_form():
    # We don't pull in a date parser for v1. Date form falls back to None
    # so the caller uses the default backoff schedule.
    assert _parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None


# ── _classify_transient unit ─────────────────────────────────────────────────


def test_classify_connection_error_transient():
    transient, delay = _classify_transient(requests.exceptions.ConnectionError("boom"))
    assert transient is True
    assert delay is None  # no Retry-After → use default backoff


def test_classify_timeout_transient():
    assert _classify_transient(requests.exceptions.Timeout("slow"))[0] is True


def test_classify_5xx_transient():
    transient, _ = _classify_transient(_http_error(503))
    assert transient is True


def test_classify_429_honors_retry_after():
    transient, delay = _classify_transient(_http_error(429, retry_after="7"))
    assert transient is True
    assert delay == 7.0


def test_classify_429_clamps_retry_after():
    transient, delay = _classify_transient(_http_error(429, retry_after="9999"))
    assert transient is True
    assert delay == float(_RETRY_AFTER_CAP_S)


def test_classify_404_permanent():
    # Wrong project_id / job_id — config error. Don't retry; propagate so the
    # operator sees the failure on the first attempt.
    transient, _ = _classify_transient(_http_error(404))
    assert transient is False


def test_classify_401_403_permanent():
    assert _classify_transient(_http_error(401))[0] is False
    assert _classify_transient(_http_error(403))[0] is False


def test_classify_filenotfound_permanent():
    # Subclass of OSError but a config issue, not transient.
    assert _classify_transient(FileNotFoundError("/no/such"))[0] is False


def test_classify_permissionerror_permanent():
    assert _classify_transient(PermissionError("denied"))[0] is False


def test_classify_oserror_eagain_transient():
    exc = OSError(errno.EAGAIN, "try again")
    assert _classify_transient(exc)[0] is True


def test_classify_oserror_eio_transient():
    exc = OSError(errno.EIO, "I/O error")
    assert _classify_transient(exc)[0] is True


def test_classify_oserror_unknown_errno_permanent():
    # An OSError with an errno NOT in our whitelist — be conservative,
    # propagate. False positives in the whitelist would mask real bugs.
    exc = OSError(errno.EINVAL, "bad arg")
    assert _classify_transient(exc)[0] is False


def test_classify_unrelated_exception_permanent():
    # Anything not specifically classified as transient must propagate so
    # programmer errors and unexpected failure modes surface immediately.
    assert _classify_transient(ValueError("bad config"))[0] is False
    assert _classify_transient(RuntimeError("oops"))[0] is False


# ── fetch_trace node integration ─────────────────────────────────────────────


def test_first_attempt_success_zero_retries(monkeypatch):
    monkeypatch.setattr("graph.nodes.fetch_trace.time.sleep", lambda s: None)
    provider = _Provider(exceptions=[], success_value="hello trace")
    out = fetch_trace(_state(provider))
    assert out["trace"] == "hello trace"
    assert out["fetch_trace_retries"] == 0
    assert provider.call_count == 1


def test_one_transient_then_success(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("graph.nodes.fetch_trace.time.sleep", lambda s: sleeps.append(s))
    provider = _Provider(
        exceptions=[requests.exceptions.ConnectionError("blip")],
        success_value="ok",
    )
    out = fetch_trace(_state(provider))
    assert out["fetch_trace_retries"] == 1
    assert provider.call_count == 2
    assert sleeps == [_RETRY_DELAYS[0]]


def test_retry_after_overrides_default_backoff(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("graph.nodes.fetch_trace.time.sleep", lambda s: sleeps.append(s))
    provider = _Provider(exceptions=[_http_error(429, retry_after="4")])
    out = fetch_trace(_state(provider))
    assert out["fetch_trace_retries"] == 1
    # Server said 4s — we slept 4s, not the default 1s.
    assert sleeps == [4.0]


def test_two_transients_then_success(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("graph.nodes.fetch_trace.time.sleep", lambda s: sleeps.append(s))
    provider = _Provider(
        exceptions=[
            requests.exceptions.Timeout("t1"),
            _http_error(503),
        ],
    )
    out = fetch_trace(_state(provider))
    assert out["fetch_trace_retries"] == 2
    assert provider.call_count == 3
    assert sleeps == [_RETRY_DELAYS[0], _RETRY_DELAYS[1]]


def test_retries_exhausted_propagates(monkeypatch):
    monkeypatch.setattr("graph.nodes.fetch_trace.time.sleep", lambda s: None)
    # Three transients = exhausts our 1 + 2 attempts.
    provider = _Provider(
        exceptions=[
            requests.exceptions.ConnectionError("1"),
            requests.exceptions.ConnectionError("2"),
            requests.exceptions.ConnectionError("3"),
        ],
    )
    with pytest.raises(requests.exceptions.ConnectionError):
        fetch_trace(_state(provider))
    assert provider.call_count == 3  # 1 initial + 2 retries


def test_permanent_error_propagates_immediately(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("graph.nodes.fetch_trace.time.sleep", lambda s: sleeps.append(s))
    provider = _Provider(exceptions=[_http_error(404)])
    with pytest.raises(requests.exceptions.HTTPError):
        fetch_trace(_state(provider))
    # No sleep should ever happen — we propagate on the first attempt.
    assert provider.call_count == 1
    assert sleeps == []


def test_filenotfound_propagates_immediately(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("graph.nodes.fetch_trace.time.sleep", lambda s: sleeps.append(s))
    provider = _Provider(exceptions=[FileNotFoundError("/no/such")])
    with pytest.raises(FileNotFoundError):
        fetch_trace(_state(provider))
    assert provider.call_count == 1
    assert sleeps == []


def test_oserror_transient_then_success(monkeypatch):
    monkeypatch.setattr("graph.nodes.fetch_trace.time.sleep", lambda s: None)
    provider = _Provider(exceptions=[OSError(errno.EAGAIN, "try again")])
    out = fetch_trace(_state(provider))
    assert out["fetch_trace_retries"] == 1


def test_unrelated_exception_propagates_immediately(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("graph.nodes.fetch_trace.time.sleep", lambda s: sleeps.append(s))
    provider = _Provider(exceptions=[ValueError("bad arg")])
    with pytest.raises(ValueError):
        fetch_trace(_state(provider))
    assert provider.call_count == 1
    assert sleeps == []
