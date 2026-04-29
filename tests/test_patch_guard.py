"""Unit tests for the apply-time patch-scope guardrail."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# bf_worker uses src-layout-style imports (`from services.patch_guard import …`),
# so add bf_worker/ to sys.path the same way the entry points do.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from services.patch_guard import (  # noqa: E402
    PatchScopeError,
    validate_patch_scope,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("y = 2\n")
    return tmp_path


def _fix(line: int = 1) -> dict:
    return {"line_number": line, "original_line": "", "new_line": ""}


# ── happy paths ───────────────────────────────────────────────────────────────

def test_allows_in_repo_path(repo: Path) -> None:
    validate_patch_scope(repo, {"app.py": [_fix()]})


def test_allows_nested_path(repo: Path) -> None:
    validate_patch_scope(repo, {"pkg/mod.py": [_fix()]})


def test_allows_at_caps(repo: Path) -> None:
    files = {f"f{i}.py": [_fix()] for i in range(5)}
    validate_patch_scope(repo, files)  # exactly max_files=5

    validate_patch_scope(repo, {"app.py": [_fix(i) for i in range(1, 51)]})  # exactly 50


# ── path containment ──────────────────────────────────────────────────────────

def test_rejects_parent_traversal(repo: Path) -> None:
    with pytest.raises(PatchScopeError, match="escapes repo root"):
        validate_patch_scope(repo, {"../etc/passwd": [_fix()]})


def test_rejects_nested_parent_traversal(repo: Path) -> None:
    with pytest.raises(PatchScopeError, match="escapes repo root"):
        validate_patch_scope(repo, {"pkg/../../outside.py": [_fix()]})


def test_rejects_absolute_path(repo: Path) -> None:
    with pytest.raises(PatchScopeError, match="absolute"):
        validate_patch_scope(repo, {"/etc/passwd": [_fix()]})


@pytest.mark.skipif(sys.platform == "win32", reason="symlink semantics differ on Windows")
def test_rejects_symlink_escape(repo: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    """A symlink inside the repo pointing outside must not let the patch escape."""
    outside = tmp_path_factory.mktemp("outside")
    (outside / "target.txt").write_text("secret")
    link = repo / "evil"
    os.symlink(outside, link)

    with pytest.raises(PatchScopeError, match="escapes repo root"):
        validate_patch_scope(repo, {"evil/target.txt": [_fix()]})


# ── deny globs ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "rel_path,expected_glob",
    [
        (".env",                 ".env"),
        (".env.local",           ".env.*"),
        ("config.env",           "*.env"),
        (".git/config",          ".git"),
        (".ssh/authorized_keys", ".ssh"),
        ("id_rsa",               "id_rsa"),
        ("server.pem",           "*.pem"),
        ("api.key",              "*.key"),
        ("credentials.json",     "credentials.*"),
        ("pkg/credentials.yml",  "*credentials*"),
        ("pkg/aws_secrets.txt",  "*secrets*"),
    ],
)
def test_rejects_sensitive_paths(repo: Path, rel_path: str, expected_glob: str) -> None:
    # Materialize the file so resolve() doesn't fail on a missing path component.
    target = repo / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("")

    with pytest.raises(PatchScopeError) as exc:
        validate_patch_scope(repo, {rel_path: [_fix()]})
    # The matching glob is included in the message so the LLM can self-correct.
    assert expected_glob in str(exc.value)


# ── caps ──────────────────────────────────────────────────────────────────────

def test_rejects_over_max_files(repo: Path) -> None:
    files = {f"f{i}.py": [_fix()] for i in range(6)}
    with pytest.raises(PatchScopeError, match="exceeds cap of 5"):
        validate_patch_scope(repo, files)


def test_rejects_over_max_lines(repo: Path) -> None:
    fixes = [_fix(i) for i in range(1, 52)]
    with pytest.raises(PatchScopeError, match="exceeds cap of 50"):
        validate_patch_scope(repo, {"app.py": fixes})


def test_caps_are_tunable(repo: Path) -> None:
    with pytest.raises(PatchScopeError, match="exceeds cap of 1"):
        validate_patch_scope(
            repo,
            {"app.py": [_fix()], "pkg/mod.py": [_fix()]},
            max_files=1,
        )


# ── input validation ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["", None])
def test_rejects_invalid_path_value(repo: Path, bad) -> None:
    with pytest.raises(PatchScopeError, match="invalid file_path"):
        validate_patch_scope(repo, {bad: [_fix()]})
