"""
Node: fetch_trace
Pulls the raw CI job trace text via the provider and stores it in state.

Wraps `provider.fetch_trace()` in a narrow transient-retry loop so a single
network blip or short-lived I/O contention doesn't abort the whole run before
any other node has a chance to do useful work.

Retry policy (mirrors the precedent in `react_loop._invoke_llm_with_retry`):
  - 2 retries on top of 1 attempt → up to 3 calls total
  - default backoff (1s, 2s); overridden by HTTP `Retry-After` when the
    server provides one (clamped at `_RETRY_AFTER_CAP_S` = 30s)
  - only known-transient exceptions trigger a retry; anything else
    propagates immediately so config / auth / wrong-id errors surface fast

Telemetry: `fetch_trace_retries` (int) is written into state on success and
surfaced via RunRecord, parallel to `parse_trace_fallback` /
`source_fetch_failed`. 0 means first-attempt success.
"""

from __future__ import annotations
import errno
import logging
import time

from graph.state import BugFixState

logger = logging.getLogger(__name__)


# Retry delays (seconds) before each retry of a transient fetch_trace call.
# Length determines max retries; (1, 2) → up to 2 retries on top of 1 attempt.
_RETRY_DELAYS = (1, 2)

# Upper bound for `Retry-After` we'll honor. Servers occasionally return very
# large values (300+); we'd rather give up and let the caller see the failure
# than block the whole run on a single blocked request.
_RETRY_AFTER_CAP_S = 30

# OSError errnos we treat as transient. Permission/missing-file errors are
# OSError subclasses with their own classes (PermissionError, FileNotFoundError,
# ...) and are filtered out before this set is consulted.
_TRANSIENT_OS_ERRNOS = frozenset({
    errno.EAGAIN,    # resource temporarily unavailable
    errno.EBUSY,     # device / resource busy
    errno.EIO,       # generic I/O error (NFS, USB, transient block-layer)
    errno.ENFILE,    # system-wide fd table full
    errno.EMFILE,    # per-process fd limit hit
    errno.ENOMEM,    # out of memory
    errno.ETIMEDOUT, # operation timed out
})


def _parse_retry_after(value) -> float | None:
    """Parse the HTTP Retry-After header.

    Supports the integer-seconds form (the only form GitLab and most APIs
    use). The HTTP-date form is rare in practice; we treat it as "use default
    backoff" rather than pulling in a date parser. Negative / non-numeric
    values are also rejected to None. Returns the (clamped) delay in seconds
    or None if no usable value can be extracted.
    """
    if not value:
        return None
    try:
        delay = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if delay <= 0:
        return None
    return float(min(delay, _RETRY_AFTER_CAP_S))


def _classify_transient(exc: BaseException) -> tuple[bool, float | None]:
    """Decide whether `exc` is a transient I/O / network failure worth retrying.

    Returns (is_transient, suggested_delay). suggested_delay is the
    server-recommended wait in seconds (from Retry-After) or None to fall
    back to the default backoff schedule.

    Imports for `requests` are deferred so this module is usable in test
    environments / providers that don't install requests.
    """
    # ── HTTP layer (GitLab provider) ──────────────────────────────────────────
    try:
        import requests
    except ImportError:        # pragma: no cover — requests is a hard dep in production
        requests = None        # type: ignore[assignment]

    if requests is not None:
        # Connection-level: TCP reset, DNS fail, read mid-stream.
        if isinstance(exc, (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        )):
            return True, None

        # HTTP status: 5xx is server-side flake, 429 is rate limit
        # (honor Retry-After). 4xx other than 429 is config/auth — propagate.
        if isinstance(exc, requests.exceptions.HTTPError):
            resp = getattr(exc, "response", None)
            status = getattr(resp, "status_code", None)
            if status == 429 or (status is not None and 500 <= status < 600):
                ra = None
                if resp is not None:
                    ra = _parse_retry_after(resp.headers.get("Retry-After"))
                return True, ra
            return False, None

    # ── Filesystem layer (Local providers) ────────────────────────────────────
    # Permission / missing-file / wrong-shape errors are config issues, not
    # transient. Filter them out before consulting the errno whitelist.
    if isinstance(exc, (FileNotFoundError, PermissionError,
                        IsADirectoryError, NotADirectoryError)):
        return False, None
    if isinstance(exc, OSError):
        if exc.errno in _TRANSIENT_OS_ERRNOS:
            return True, None
        return False, None

    return False, None


def fetch_trace(state: BugFixState) -> BugFixState:
    provider = state["provider"]
    project_id = state.get("project_id", "")
    job_id = state.get("job_id", "")

    attempts = 1 + len(_RETRY_DELAYS)
    retries = 0
    for i in range(attempts):
        try:
            trace = provider.fetch_trace(project_id=project_id, job_id=job_id)
            logger.info("trace fetched (%d chars, retries=%d)", len(trace), retries)
            return {"trace": trace, "fetch_trace_retries": retries}
        except Exception as exc:
            transient, suggested = _classify_transient(exc)
            if not transient or i == attempts - 1:
                # Either permanent (raise so config error surfaces fast) or
                # we've exhausted the retry budget on a transient. Either way
                # the run aborts at the agent boundary.
                if transient:
                    logger.error(
                        "fetch_trace: transient %s exhausted retries (%d): %s",
                        type(exc).__name__, retries, exc,
                    )
                raise
            delay = suggested if suggested is not None else _RETRY_DELAYS[i]
            logger.warning(
                "fetch_trace: transient %s; retry %d/%d in %.1fs%s: %s",
                type(exc).__name__, i + 1, len(_RETRY_DELAYS), delay,
                " (Retry-After)" if suggested is not None else "",
                exc,
            )
            time.sleep(delay)
            retries += 1
