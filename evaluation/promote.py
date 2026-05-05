"""Helpers for promoting journal entries into evaluation fixtures."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def base_commit_from_record(record: dict) -> str:
    """Return the buggy-source commit captured before the fix branch was made."""
    commit = record.get("base_commit")
    if not commit:
        branch_result = record.get("branch_create_result") or {}
        commit = branch_result.get("commit")
    if commit in (None, "", "none"):
        return ""
    return str(commit)


def repo_from_record(record: dict, explicit_repo: str | None = None) -> str:
    """Return a repo URL/path suitable for cloning, preferring explicit input."""
    if explicit_repo:
        return explicit_repo
    return (
        record.get("source_repo_path")
        or record.get("source_dir")
        or record.get("project_web_url")
        or ""
    )


def populate_source_from_git(
    *,
    record: dict,
    source_dir: Path,
    explicit_repo: str | None = None,
) -> tuple[bool, str]:
    """Populate fixture source/ from the recorded buggy git commit.

    Returns ``(True, message)`` on success and ``(False, reason)`` when the
    journal does not contain enough git information or git reconstruction fails.
    """
    commit = base_commit_from_record(record)
    if not commit:
        return False, "journal record has no base_commit"

    repo = repo_from_record(record, explicit_repo)
    if not repo:
        return False, "journal record has no source repo URL/path"

    source_dir.mkdir(parents=True, exist_ok=True)
    if any(source_dir.iterdir()):
        return False, f"source directory is not empty: {source_dir}"

    work_root = Path(tempfile.mkdtemp(prefix="sdlcma_promote_"))
    clone_dir = work_root / "repo"
    try:
        clone_url = _clone_url(repo)
        _run_git(["clone", "--no-checkout", clone_url, str(clone_dir)], cwd=work_root, timeout=300)
        _run_git(["checkout", commit], cwd=clone_dir, timeout=120)
        _copy_tree_without_git(clone_dir, source_dir)
        return True, f"source populated from {repo} at {commit}"
    except Exception as exc:
        return False, str(exc)
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


def _clone_url(repo: str) -> str:
    """Adapt GitLab web URLs to the local clone shape when settings are present."""
    if repo.startswith(("http://", "https://")):
        try:
            from settings import worker_cfg as cfg
        except Exception:
            cfg = None

        if cfg is not None and getattr(cfg, "env", "") == "local_multi_process":
            repo = repo.replace("gitlab.local", "localhost:8080")

        if cfg is not None and "@" not in urlsplit(repo).netloc:
            token = getattr(cfg, "gitlab_private_token", "")
            username = getattr(cfg, "gitlab_username", "")
            if token and username:
                parts = urlsplit(repo)
                netloc = f"{username}:{token}@{parts.netloc}"
                repo = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    return repo


def _run_git(args: list[str], *, cwd: Path, timeout: int) -> str:
    env = os.environ.copy()
    env.setdefault(
        "GIT_SSH_COMMAND",
        "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
    )
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({proc.returncode}): {_mask_secret(proc.stderr.strip())}"
        )
    return proc.stdout.strip()


def _copy_tree_without_git(src: Path, dst: Path) -> None:
    for child in src.iterdir():
        if child.name == ".git":
            continue
        target = dst / child.name
        if child.is_dir():
            shutil.copytree(child, target, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
            _make_writable(target)
        else:
            shutil.copy2(child, target)
            _make_writable(target)


def _make_writable(path: Path) -> None:
    if path.is_dir():
        for child in path.rglob("*"):
            try:
                child.chmod(child.stat().st_mode | stat.S_IWRITE)
            except OSError:
                pass
    try:
        path.chmod(path.stat().st_mode | stat.S_IWRITE)
    except OSError:
        pass


def _mask_secret(text: str) -> str:
    try:
        from settings import worker_cfg as cfg
    except Exception:
        return text
    token = getattr(cfg, "gitlab_private_token", "")
    return text.replace(token, "***") if token else text
