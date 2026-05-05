from __future__ import annotations

import subprocess
from pathlib import Path

from evaluation.promote import base_commit_from_record, populate_source_from_git


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_populate_source_from_git_uses_base_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    (repo / "app.py").write_text("buggy\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "buggy")
    buggy_commit = _git(repo, "rev-parse", "HEAD")

    (repo / "app.py").write_text("fixed\n", encoding="utf-8")
    _git(repo, "commit", "-am", "fixed")

    source_dir = tmp_path / "fixture" / "source"
    ok, message = populate_source_from_git(
        record={"base_commit": buggy_commit},
        source_dir=source_dir,
        explicit_repo=str(repo),
    )

    assert ok, message
    assert (source_dir / "app.py").read_text(encoding="utf-8") == "buggy\n"
    assert not (source_dir / ".git").exists()


def test_base_commit_falls_back_to_branch_create_result() -> None:
    assert (
        base_commit_from_record({"branch_create_result": {"commit": "abc123"}})
        == "abc123"
    )
