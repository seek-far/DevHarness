"""Generated trees (.venv, caches) must never leak into a fix's output.

Two gates, one per local mode:
  * git mode    — `_ensure_venv_excluded_from_git` writes `.git/info/exclude`
                  so `git add -A` skips the venv.
  * no-git mode — `LocalNoGitProvider.commit_and_push` prunes the os.walk, so
                  the generated patch contains only real source changes.

The no-git gate was missing until 2026-07-14: `apply_change_and_test` creates
`.venv/` and `.pytest_cache/` inside the work dir, they don't exist in the
user's source dir, so the differ classed every file under them as "new" and
inlined it. A run whose real content was a 3-line fix emitted a **39 MB**
patch. Found while smoke-testing the Azure backend.
"""

from __future__ import annotations

import sys
from pathlib import Path

# bf_worker uses src-layout-style imports (`from providers... import …`),
# so add bf_worker/ to sys.path the same way the entry points do.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from providers.local_provider import (  # noqa: E402
    LocalNoGitProvider,
    _ensure_venv_excluded_from_git,
)


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


# ── no-git mode: the patch must contain only real source changes ─────────────


def _no_git_run(tmp_path: Path) -> tuple[LocalNoGitProvider, Path, Path]:
    """Build a source dir + a work dir that looks like a post-test run:
    one real edit, plus the .venv/ and .pytest_cache/ that apply_change_and_test
    creates inside the work dir."""
    source = tmp_path / "source"
    work = tmp_path / "work"
    out = tmp_path / "out"
    source.mkdir()
    work.mkdir()

    (source / "last_n.py").write_text("def get_last_n(items, n):\n    return items[-n:]\n")
    (work / "last_n.py").write_text(
        "def get_last_n(items, n):\n    if n <= 0:\n        return []\n    return items[-n:]\n"
    )

    # Generated trees — present in work, absent from source.
    for d, fname, body in [
        (".venv/lib/python3.12/site-packages", "typing_extensions.py", "x = 1\n" * 5000),
        (".pytest_cache/v/cache", "nodeids", '["test_last_n.py::test_basic"]'),
        ("__pycache__", "last_n.cpython-312.pyc", "\x00binary\x00"),
    ]:
        p = work / d
        p.mkdir(parents=True)
        (p / fname).write_text(body)

    provider = LocalNoGitProvider(
        source_dir=str(source), output_dir=str(out), bug_id="BUG-1"
    )
    return provider, work, out


def test_no_git_patch_excludes_generated_trees(tmp_path: Path) -> None:
    provider, work, out = _no_git_run(tmp_path)

    result = provider.commit_and_push(work, "fix")

    assert result["status"] == "success"
    patch = (out / "BUG-1.patch").read_text()

    # The real fix is there…
    assert "+    if n <= 0:" in patch
    assert "a/last_n.py" in patch

    # …and nothing else is.
    for leaked in (".venv", ".pytest_cache", "__pycache__", "typing_extensions"):
        assert leaked not in patch, f"{leaked!r} leaked into the generated patch"


def test_no_git_patch_stays_small(tmp_path: Path) -> None:
    """Size is the symptom operators actually notice: the real regression
    produced a 39 MB patch for a 3-line fix. A source-only diff is bytes."""
    provider, work, out = _no_git_run(tmp_path)

    provider.commit_and_push(work, "fix")

    assert (out / "BUG-1.patch").stat().st_size < 2000


def test_no_git_reports_no_changes_when_only_generated_trees_differ(tmp_path: Path) -> None:
    """If the LLM changed nothing, creating a venv must not look like a fix."""
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    work.mkdir()
    (source / "a.py").write_text("x = 1\n")
    (work / "a.py").write_text("x = 1\n")
    (work / ".venv").mkdir()
    (work / ".venv" / "pyvenv.cfg").write_text("home = /usr\n")

    provider = LocalNoGitProvider(
        source_dir=str(source), output_dir=str(tmp_path / "out"), bug_id="BUG-2"
    )

    assert provider.commit_and_push(work, "fix")["status"] == "no_changes"


# ── the .gitignore the harness writes must not become part of the patch ──────


def test_gitignore_written_in_git_mode(tmp_path: Path) -> None:
    from graph.nodes.apply_change_and_test import ensure_venv_gitignored

    (tmp_path / ".git").mkdir()

    assert ensure_venv_gitignored(tmp_path) is True
    assert ".venv/" in (tmp_path / ".gitignore").read_text()


def test_gitignore_not_written_in_no_git_mode(tmp_path: Path) -> None:
    """No `git add` here, so the entry buys nothing — and creating the file
    invents a change the user never made, which lands in their patch."""
    from graph.nodes.apply_change_and_test import ensure_venv_gitignored

    assert ensure_venv_gitignored(tmp_path) is False
    assert not (tmp_path / ".gitignore").exists()


def test_gitignore_write_is_idempotent(tmp_path: Path) -> None:
    from graph.nodes.apply_change_and_test import ensure_venv_gitignored

    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("*.log\n.venv/\n")

    assert ensure_venv_gitignored(tmp_path) is False
    assert (tmp_path / ".gitignore").read_text() == "*.log\n.venv/\n"


def test_gitignore_appends_to_existing_git_repo_file(tmp_path: Path) -> None:
    from graph.nodes.apply_change_and_test import ensure_venv_gitignored

    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("*.log\n")

    assert ensure_venv_gitignored(tmp_path) is True
    body = (tmp_path / ".gitignore").read_text()
    assert "*.log" in body and ".venv/" in body
