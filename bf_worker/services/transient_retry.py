"""
services/transient_retry.py

Narrow transient-retry layer for provider I/O calls inside graph nodes.

The same shape (1 attempt + 2 retries, default backoff `(1s, 2s)`, honour
HTTP `Retry-After` clamped at 30s) is applied at every provider call site
that hits the network or filesystem: `fetch_trace`, `fetch_source_file`,
`commit_change`, `wait_ci_result`, `create_mr`. The classifier is
deliberately narrow — only known-transient exceptions trigger a retry; any
other failure (auth, 4xx, FileNotFoundError, …) propagates immediately so
configuration errors surface fast.

Each call site records the number of retries it took into a state field
(e.g. `fetch_trace_retries`, `commit_change_retries`) so the journal /
RunRecord captures the cost of transient recovery.
"""

from __future__ import annotations

import errno
import logging
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)


# Default retry delays (seconds). Length determines max retries; (1, 2) means
# up to 2 retries on top of 1 attempt → 3 calls maximum.
DEFAULT_RETRY_DELAYS: tuple[int, ...] = (1, 2)

# Upper bound on `Retry-After` we'll honour. Servers occasionally return
# multi-minute values; we'd rather give up and surface the failure than block
# the entire run on a single rate-limit response.
RETRY_AFTER_CAP_S = 30

# OSError errnos we treat as transient. PermissionError and FileNotFoundError
# are OSError subclasses with their own classes and are filtered out before
# this set is consulted.
_TRANSIENT_OS_ERRNOS = frozenset({
    errno.EAGAIN,    # resource temporarily unavailable
    errno.EBUSY,     # device / resource busy
    errno.EIO,       # generic I/O error (NFS, USB, transient block-layer)
    errno.ENFILE,    # system-wide fd table full
    errno.EMFILE,    # per-process fd limit hit
    errno.ENOMEM,    # out of memory
    errno.ETIMEDOUT, # operation timed out
})

T = TypeVar("T")


def parse_retry_after(value) -> float | None:
    """Parse the HTTP Retry-After header.

    Supports the integer-seconds form (the only form GitLab and most APIs
    use). The HTTP-date form is rare in practice; we treat it as "use default
    backoff" rather than pulling in a date parser. Negative / non-numeric /
    zero values fall back to None. Returns the (clamped) delay in seconds.
    """
    if not value:
        return None
    try:
        delay = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if delay <= 0:
        return None
    return float(min(delay, RETRY_AFTER_CAP_S))


def classify_transient(exc: BaseException) -> tuple[bool, float | None]:
    """Decide whether `exc` is a transient I/O / network failure worth retrying.

    Returns (is_transient, suggested_delay). `suggested_delay` is the
    server-recommended wait in seconds (from a `Retry-After` header) or None
    to fall back to the default backoff schedule.

    The `requests` import is deferred so this module is usable in test
    environments / providers that don't ship requests.
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

        # HTTP status: 5xx is server-side flake, 429 is rate limit (honour
        # Retry-After). 4xx other than 429 is config/auth — propagate.
        if isinstance(exc, requests.exceptions.HTTPError):
            resp = getattr(exc, "response", None)
            status = getattr(resp, "status_code", None)
            if status == 429 or (status is not None and 500 <= status < 600):
                ra = None
                if resp is not None:
                    ra = parse_retry_after(resp.headers.get("Retry-After"))
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

    # ── Redis (orchestrator inbox / heartbeat) ────────────────────────────────
    # redis-py exceptions come from a separate package; deferred import for
    # the same reason as `requests`.
    try:
        import redis.exceptions as _redis_exc
    except ImportError:        # pragma: no cover — redis is a hard dep in production
        _redis_exc = None      # type: ignore[assignment]

    if _redis_exc is not None:
        if isinstance(exc, (
            _redis_exc.ConnectionError,
            _redis_exc.TimeoutError,
            _redis_exc.BusyLoadingError,
        )):
            return True, None

    return False, None


def with_transient_retry(
    fn: Callable[[], T],
    *,
    op_name: str,
    delays: tuple[int, ...] = DEFAULT_RETRY_DELAYS,
) -> tuple[T, int]:
    """Call `fn()` with narrow transient-retry semantics.

    Returns `(result, retries)` on success — `retries` is the number of
    retries that actually fired (0 = first-attempt success, len(delays) = max).
    Re-raises immediately on permanent errors and after retries are exhausted
    on transient ones.

    `op_name` is used only for log messages so distinct call sites can be
    distinguished in operator logs.
    """
    attempts = 1 + len(delays)
    retries = 0
    for i in range(attempts):
        try:
            return fn(), retries
        except Exception as exc:
            transient, suggested = classify_transient(exc)
            if not transient or i == attempts - 1:
                if transient:
                    logger.error(
                        "%s: transient %s exhausted retries (%d): %s",
                        op_name, type(exc).__name__, retries, exc,
                    )
                raise
            delay = suggested if suggested is not None else delays[i]
            logger.warning(
                "%s: transient %s; retry %d/%d in %.1fs%s: %s",
                op_name, type(exc).__name__, i + 1, len(delays), delay,
                " (Retry-After)" if suggested is not None else "",
                exc,
            )
            time.sleep(delay)
            retries += 1
    # Unreachable — the loop always returns or raises.
    raise RuntimeError(f"{op_name}: with_transient_retry fell through")  # pragma: no cover
