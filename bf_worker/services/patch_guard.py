"""
services/patch_guard.py

Apply-time validation of LLM-proposed patches.

The LLM has unrestricted authority over `submit_fix.fixes[].file_path`, so a
hallucinated path or prompt-injected trace could cause a write outside the
repo or onto a sensitive file. This module is the last line of defense
between the LLM's intent and the filesystem.

Public API:
    PatchScopeError       — raised on any guardrail violation
    validate_patch_scope  — checks paths and caps, raises on violation
"""

from __future__ import annotations

import fnmatch
from pathlib import Path


class PatchScopeError(Exception):
    """Raised when an LLM-proposed patch violates the apply-time guardrail."""


# Sensitive path patterns. Matched against the repo-relative POSIX path.
# Globs use fnmatch semantics; "**" is treated as "any path segments".
DENY_GLOBS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.env",
    ".git",
    ".git/*",
    ".ssh",
    ".ssh/*",
    "id_rsa",
    "id_rsa.*",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_ed25519.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    ".aws/*",
    ".gnupg/*",
    "credentials",
    "credentials.*",
    "*credentials*",
    "secrets",
    "secrets.*",
    "*secrets*",
)


def _is_absolute_path(path: str) -> bool:
    """Return True for POSIX or Windows absolute path strings."""
    return Path(path).is_absolute() or path.startswith(("/", "\\")) or (
        len(path) >= 2 and path[1] == ":"
    )


def matches_deny(rel_posix: str) -> str | None:
    """Return the matching glob if `rel_posix` is denied, else None."""
    # Check the full path and every parent segment, so e.g. ".git/config"
    # is rejected via the ".git/*" rule and ".git" via the ".git" rule.
    parts = rel_posix.split("/")
    for i in range(len(parts)):
        candidate = "/".join(parts[: i + 1])
        for glob in DENY_GLOBS:
            if fnmatch.fnmatchcase(candidate, glob):
                return glob
    # Also match the basename as a convenience (e.g. "deep/nested/secrets.yml"
    # against "*secrets*").
    basename = parts[-1]
    for glob in DENY_GLOBS:
        if "/" not in glob and fnmatch.fnmatchcase(basename, glob):
            return glob
    return None


def validate_patch_scope(
    repo_path: Path | str,
    fixes_by_file: dict[str, list[dict]],
    *,
    max_files: int = 5,
    max_lines: int = 50,
) -> None:
    """Validate the LLM patch against scope guardrails.

    Raises PatchScopeError on the first violation. Does not write anything.

    Checks (in order):
      1. file count cap (max_files)
      2. for each file:
         - non-empty path
         - resolved path stays inside repo_path (after symlink resolution)
         - repo-relative path does not match a sensitive deny glob
      3. total line-edit count cap (max_lines)
    """
    repo_root = Path(repo_path).resolve()

    if len(fixes_by_file) > max_files:
        raise PatchScopeError(
            f"patch touches {len(fixes_by_file)} files, exceeds cap of {max_files}"
        )

    total_lines = 0
    for rel_path, fixes in fixes_by_file.items():
        if not rel_path or not isinstance(rel_path, str):
            raise PatchScopeError(f"invalid file_path: {rel_path!r}")

        # Reject absolute paths up front so the message is clear.
        if _is_absolute_path(rel_path):
            raise PatchScopeError(
                f"file_path must be repo-relative, got absolute: {rel_path!r}"
            )

        # Resolve via the repo root and check containment. `resolve()` follows
        # symlinks, which closes the symlink-escape vector.
        target = (repo_root / rel_path).resolve()
        try:
            target.relative_to(repo_root)
        except ValueError:
            raise PatchScopeError(
                f"file_path escapes repo root: {rel_path!r} -> {target}"
            )

        rel_posix = target.relative_to(repo_root).as_posix()
        denied_by = matches_deny(rel_posix)
        if denied_by is not None:
            raise PatchScopeError(
                f"file_path matches sensitive deny glob '{denied_by}': {rel_posix}"
            )

        total_lines += len(fixes)

    if total_lines > max_lines:
        raise PatchScopeError(
            f"patch edits {total_lines} lines, exceeds cap of {max_lines}"
        )
