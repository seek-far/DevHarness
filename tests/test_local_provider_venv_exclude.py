"""Unit tests for _ensure_venv_excluded_from_git in local_provider."""

from __future__ import annotations

import sys
from pathlib import Path

# bf_worker uses src-layout-style imports (`from providers... import …`),
# so add bf_worker/ to sys.path the same way the entry points do.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from providers.local_provider import _ensure_venv_excluded_from_git  # noqa: E402


def _make_git_repo(path: Path) -> Path:
    (path / ".git" / "info").mkdir(parents=True)
    return path / ".git" / "info" / "exclude"


def test_appends_to_empty_exclude_in_git_repo(tmp_path: Path) -> None:
    exclude_file = _make_git_repo(tmp_path)

    _ensure_venv_excluded_from_git(tmp_path)

    assert exclude_file.exists()
    assert ".venv/" in exclude_file.read_text().splitlines()


def test_creates_exclude_file_when_missing(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()  # info/ dir does not exist yet

    _ensure_venv_excluded_from_git(tmp_path)

    exclude_file = tmp_path / ".git" / "info" / "exclude"
    assert exclude_file.exists()
    assert ".venv/" in exclude_file.read_text().splitlines()


def test_appends_to_nonempty_exclude_with_trailing_newline(tmp_path: Path) -> None:
    exclude_file = _make_git_repo(tmp_path)
    exclude_file.write_text("*.log\nbuild/\n")

    _ensure_venv_excluded_from_git(tmp_path)

    lines = exclude_file.read_text().splitlines()
    assert lines == ["*.log", "build/", ".venv/"]


def test_appends_to_nonempty_exclude_without_trailing_newline(tmp_path: Path) -> None:
    exclude_file = _make_git_repo(tmp_path)
    exclude_file.write_text("*.log")

    _ensure_venv_excluded_from_git(tmp_path)

    lines = exclude_file.read_text().splitlines()
    assert lines == ["*.log", ".venv/"]


def test_idempotent_when_venv_already_excluded(tmp_path: Path) -> None:
    exclude_file = _make_git_repo(tmp_path)
    exclude_file.write_text(".venv/\n")

    _ensure_venv_excluded_from_git(tmp_path)
    _ensure_venv_excluded_from_git(tmp_path)

    assert exclude_file.read_text() == ".venv/\n"


def test_recognizes_alternative_venv_patterns(tmp_path: Path) -> None:
    for pattern in (".venv", "/.venv", "/.venv/"):
        repo = tmp_path / f"repo_{pattern.replace('/', '_')}"
        exclude_file = _make_git_repo(repo)
        exclude_file.write_text(f"{pattern}\n")

        _ensure_venv_excluded_from_git(repo)

        assert exclude_file.read_text() == f"{pattern}\n"


def test_ignores_commented_match(tmp_path: Path) -> None:
    exclude_file = _make_git_repo(tmp_path)
    exclude_file.write_text("# .venv/\n")

    _ensure_venv_excluded_from_git(tmp_path)

    lines = exclude_file.read_text().splitlines()
    assert "# .venv/" in lines
    assert ".venv/" in lines


def test_noop_when_not_a_git_repo(tmp_path: Path) -> None:
    _ensure_venv_excluded_from_git(tmp_path)

    assert not (tmp_path / ".git").exists()
