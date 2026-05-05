"""Shared helpers for loading agent specs in running-mode entry points."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from agents.langgraph_agent import LangGraphAgent
from enhancements import build_enhancements
from journal import JournalWriter

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_CURRENT_REFS = {"", "current", "workspace", "working-tree", "working_tree"}


def load_agent_spec(config_path: str | None) -> dict | None:
    """Load the first agent spec from a config file, if one is provided."""
    if not config_path:
        return None
    data = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data[0] if data else None
    return data


def normalize_agent_ref(spec: dict | None) -> str:
    if not spec:
        return ""
    ref = spec.get("agent_ref")
    if ref is None:
        return ""
    ref = str(ref).strip()
    return "" if ref.lower() in _CURRENT_REFS else ref


def make_agent(agent_spec: dict | None = None) -> LangGraphAgent:
    """Build the configured running-mode agent."""
    if agent_spec is None:
        return LangGraphAgent(journal=JournalWriter())

    kind = agent_spec.get("agent", "langgraph")
    kwargs = dict(agent_spec.get("kwargs", {}))
    if kind != "langgraph":
        raise ValueError(f"unknown agent kind: {kind!r}")

    enh_specs = agent_spec.get("enhancements", [])
    if enh_specs:
        kwargs.setdefault("enhancements", build_enhancements(enh_specs))
    return LangGraphAgent(journal=JournalWriter(), agent_config=agent_spec, **kwargs)


def maybe_reexec_for_agent_ref(config_path: str | None, script_relpath: str) -> int | None:
    """Run this entry point from a detached worktree when config pins agent_ref.

    Returns the child process exit code in the parent after the child has
    completed, so callers can exit without running the current checkout's agent.
    Returns None when no re-exec is needed or when already running inside the
    pinned worktree.
    """
    if os.environ.get("BF_AGENT_REF_APPLIED") == "1":
        return None

    spec = load_agent_spec(config_path)
    ref = normalize_agent_ref(spec)
    if not ref:
        return None

    resolved = _run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"])
    base_dir = Path(tempfile.gettempdir()) / "sdlcma_agent_worktrees"
    base_dir.mkdir(parents=True, exist_ok=True)
    worktree = Path(tempfile.mkdtemp(prefix=f"{_safe_ref_label(ref)}_", dir=str(base_dir)))
    worktree_created = False
    child_config: Path | None = None

    try:
        shutil.rmtree(worktree)
        _run_git(["worktree", "add", "--detach", str(worktree), resolved], timeout=120)
        worktree_created = True
        _copy_local_env_files(worktree)
        _prepare_worktree_excludes(worktree)

        child_config = Path(tempfile.gettempdir()) / (
            f"sdlcma_agent_spec_{_safe_ref_label(ref)}_{os.getpid()}.json"
        )
        child_config.write_text(json.dumps(spec, indent=2), encoding="utf-8")

        env = os.environ.copy()
        env.update(_env_values_for_child())
        env["BF_AGENT_CONFIG"] = str(child_config)
        env["BF_AGENT_REF_APPLIED"] = "1"
        env.setdefault("BF_JOURNAL_DIR", str(_ROOT / "evaluation" / "journal"))
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "status.showUntrackedFiles"
        env["GIT_CONFIG_VALUE_0"] = "no"

        logger.info("re-executing worker in agent_ref=%s commit=%s", ref, resolved)
        proc = subprocess.run(
            [sys.executable, str(worktree / script_relpath), *sys.argv[1:]],
            cwd=str(worktree),
            env=env,
            check=False,
        )
        return proc.returncode
    finally:
        if child_config is not None:
            try:
                child_config.unlink()
            except Exception:
                pass
        if worktree_created:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=str(_ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
        elif worktree.exists():
            shutil.rmtree(worktree, ignore_errors=True)


def _run_git(args: list[str], *, cwd: Path = _ROOT, timeout: int = 60) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def _safe_ref_label(ref: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "-" for c in ref)[:80] or "ref"


def _copy_local_env_files(worktree: Path) -> None:
    for rel_dir in ("settings", "gateway"):
        src_dir = _ROOT / rel_dir
        if not src_dir.is_dir():
            continue
        dst_dir = worktree / rel_dir
        dst_dir.mkdir(parents=True, exist_ok=True)
        for src in src_dir.iterdir():
            if src.is_file() and src.name != ".env" and src.name.endswith(".env"):
                shutil.copy2(src, dst_dir / src.name)


def _prepare_worktree_excludes(worktree: Path) -> None:
    exclude = worktree / ".git" / "info" / "exclude"
    git_file = worktree / ".git"
    if not exclude.exists() and git_file.is_file():
        text = git_file.read_text(encoding="utf-8", errors="ignore").strip()
        prefix = "gitdir: "
        if text.startswith(prefix):
            git_dir = Path(text[len(prefix):])
            if not git_dir.is_absolute():
                git_dir = (worktree / git_dir).resolve()
            exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    patterns = [
        "settings/.env",
        "settings/*.env",
        "gateway/*.env",
        "evaluation/runs/",
        "__pycache__/",
        "**/__pycache__/",
        "*.pyc",
    ]
    with exclude.open("a", encoding="utf-8") as f:
        for pattern in patterns:
            if pattern not in existing:
                f.write(pattern + "\n")


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip():
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _env_values_for_child() -> dict[str, str]:
    values = _read_env_file(_ROOT / "settings" / ".env")
    env_name = values.get("ENV") or values.get("env") or os.environ.get("ENV") or "local_multi_process"
    values.update(_read_env_file(_ROOT / "settings" / f"worker_{env_name}.env"))
    values.setdefault("ENV", env_name)
    return values
