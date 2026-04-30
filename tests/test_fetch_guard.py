"""Unit tests for the read-side fetch path guardrail."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from services.fetch_guard import (  # noqa: E402
    DENY_GLOBS,
    FetchScopeError,
    validate_fetch_path,
)
from services.patch_guard import DENY_GLOBS as WRITE_DENY_GLOBS  # noqa: E402


# ── happy paths ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "path",
    [
        "app.py",
        "pkg/mod.py",
        "pkg/sub/deep.py",
        "./app.py",         # leading './' is normalised away
        "tests/test_x.py",
    ],
)
def test_allows_in_repo_relative_paths(path: str) -> None:
    validate_fetch_path(path)  # no exception


# ── path containment ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "/Users/me/.aws/credentials",
        "\\windows\\path",
        "C:/Windows/System32",
    ],
)
def test_rejects_absolute_paths(path: str) -> None:
    with pytest.raises(FetchScopeError, match="absolute"):
        validate_fetch_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "../etc/passwd",
        "pkg/../../outside.py",
        "..",
        "a/b/../../../c",
    ],
)
def test_rejects_parent_traversal(path: str) -> None:
    with pytest.raises(FetchScopeError, match=r"\.\."):
        validate_fetch_path(path)


# ── deny globs ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "path,expected_glob",
    [
        (".env",                 ".env"),
        (".env.production",      ".env.*"),
        ("config.env",           "*.env"),
        (".git/HEAD",            ".git"),
        (".ssh/id_rsa",          ".ssh"),
        ("id_rsa",               "id_rsa"),
        ("server.pem",           "*.pem"),
        ("api.key",              "*.key"),
        ("credentials.json",     "credentials.*"),
        ("pkg/db_credentials.yml", "*credentials*"),
        ("pkg/aws_secrets.txt",  "*secrets*"),
    ],
)
def test_rejects_sensitive_paths(path: str, expected_glob: str) -> None:
    with pytest.raises(FetchScopeError) as exc:
        validate_fetch_path(path)
    assert expected_glob in str(exc.value)


# ── input validation ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["", None, ".", "./", "./."])
def test_rejects_invalid_input(bad) -> None:
    with pytest.raises(FetchScopeError, match="invalid"):
        validate_fetch_path(bad)


# ── shared denylist invariant ─────────────────────────────────────────────────

def test_read_and_write_share_one_denylist() -> None:
    """Read-side and write-side guards must reuse the same source of truth.

    A drift here is a security regression: the LLM could read a sensitive
    file even when it cannot write to it (or vice-versa).
    """
    assert DENY_GLOBS is WRITE_DENY_GLOBS
