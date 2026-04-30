"""
services/fetch_guard.py

Read-side counterpart to ``patch_guard``.

The LLM's ``fetch_additional_file`` and ``fetch_file_segment`` tools accept
an attacker-influenceable ``path`` argument. Without validation, a hijacked
or prompt-injected LLM could ask for ``/etc/passwd``, ``.env``, ``.git/...``,
or a path that traverses out of the repo, and the provider would happily
return it. The LLM could then leak that content via ``error_reason`` or
slip it into a patch.

This module performs **string-level** path validation before the provider
is touched. It deliberately does not resolve symlinks (the GitLab provider
has no local filesystem at all); the provider's own ``fetch_file`` is
expected to handle on-disk safety. We share the sensitive-path denylist
with ``patch_guard`` so the read and write surfaces never drift.

Public API:
    FetchScopeError       — raised on rejection
    validate_fetch_path   — checks one path string, raises on violation
"""

from __future__ import annotations

from services.patch_guard import DENY_GLOBS, matches_deny


class FetchScopeError(Exception):
    """Raised when an LLM-requested fetch path violates the read-side guard."""


def validate_fetch_path(rel_path: str) -> None:
    """Validate a repo-relative path the LLM wants to read.

    Raises ``FetchScopeError`` on:
      - empty / non-string input
      - absolute path
      - any segment equal to ``..`` (parent traversal)
      - repo-relative path matching a sensitive deny glob

    The path is **not** resolved against any filesystem here; that's the
    provider's job. This is a cheap upstream sieve so obviously malicious
    requests never reach ``provider.fetch_file()``.
    """
    if not rel_path or not isinstance(rel_path, str):
        raise FetchScopeError(f"invalid path: {rel_path!r}")

    # Reject Windows-style and POSIX-style absolute paths.
    if rel_path.startswith(("/", "\\")) or (
        len(rel_path) >= 2 and rel_path[1] == ":"
    ):
        raise FetchScopeError(
            f"path must be repo-relative, got absolute: {rel_path!r}"
        )

    # Normalise separators and reject any '..' segment. We do not collapse
    # them — '..' anywhere is a prompt-injection signal, not a typo.
    parts = rel_path.replace("\\", "/").split("/")
    if any(p == ".." for p in parts):
        raise FetchScopeError(
            f"path may not contain '..' segments: {rel_path!r}"
        )

    # Strip any leading './' segments before checking the deny list.
    rel_posix = "/".join(p for p in parts if p not in ("", "."))
    if not rel_posix:
        raise FetchScopeError(f"invalid path: {rel_path!r}")

    denied_by = matches_deny(rel_posix)
    if denied_by is not None:
        raise FetchScopeError(
            f"path matches sensitive deny glob '{denied_by}': {rel_posix}"
        )


# Re-export for callers that want to introspect the policy without reaching
# into patch_guard.
__all__ = ["FetchScopeError", "validate_fetch_path", "DENY_GLOBS"]
