"""
Evaluation coordinator for agent specs pinned to git refs.

The in-process runner can only evaluate code from the current Python process.
When an agent spec declares ``agent_ref`` (branch, tag, or commit), the
coordinator creates an isolated git worktree at that ref and asks that checkout
to run the existing evaluation CLI for the matching subset of specs. Results
are copied back into the caller's ``evaluation/runs/<run_id>/`` directory.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from evaluation.fixture import Fixture

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_RUNS_ROOT = _HERE / "runs"
_CURRENT_REFS = {"", "current", "workspace", "working-tree", "working_tree"}


def normalize_agent_ref(spec: dict) -> str:
    """Return normalized agent_ref. Empty string means current checkout."""
    ref = spec.get("agent_ref")
    if ref is None:
        return ""
    ref = str(ref).strip()
    return "" if ref.lower() in _CURRENT_REFS else ref


def group_specs_by_agent_ref(agent_specs: Iterable[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for spec in agent_specs:
        grouped[normalize_agent_ref(spec)].append(spec)
    return dict(grouped)


def run_coordinated_sweep(
    agent_specs: list[dict],
    fixtures: list[Fixture],
    run_id: str | None = None,
) -> Path:
    """Run specs in the current checkout or isolated worktrees by agent_ref."""
    run_id = run_id or datetime.now(timezone.utc).strftime("run_%Y%m%dT%H%M%SZ")
    run_dir = _RUNS_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    grouped = group_specs_by_agent_ref(agent_specs)
    fixture_ids = [f.fixture_id for f in fixtures]

    if "" in grouped:
        from evaluation.runner import run_sweep

        logger.info("coordinator: running %d current-checkout spec(s)", len(grouped[""]))
        run_sweep(grouped[""], fixtures, run_id=run_id)

    for ref, specs in sorted((k, v) for k, v in grouped.items() if k):
        logger.info("coordinator: running %d spec(s) in worktree ref=%s", len(specs), ref)
        _run_specs_in_worktree(ref=ref, specs=specs, run_id=run_id, fixture_ids=fixture_ids)

    _merge_summary(run_dir)
    return run_dir


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


def _run_specs_in_worktree(
    *,
    ref: str,
    specs: list[dict],
    run_id: str,
    fixture_ids: list[str],
) -> None:
    resolved = _run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"])
    base_dir = Path(tempfile.gettempdir()) / "sdlcma_eval_worktrees"
    base_dir.mkdir(parents=True, exist_ok=True)
    worktree = Path(tempfile.mkdtemp(prefix=f"{_safe_ref_label(ref)}_", dir=str(base_dir)))
    config_path = Path(tempfile.gettempdir()) / f"sdlcma_eval_specs_{_safe_ref_label(ref)}_{run_id}.json"
    worktree_created = False
    try:
        shutil.rmtree(worktree)
        _run_git(["worktree", "add", "--detach", str(worktree), resolved], timeout=120)
        worktree_created = True
        _copy_local_env_files(worktree)
        _prepare_worktree_excludes(worktree)

        config_path.write_text(json.dumps(specs, indent=2), encoding="utf-8")

        cmd = [
            sys.executable,
            "-m",
            "evaluation.cli",
            "run",
            "--config",
            str(config_path),
            "--run-id",
            run_id,
        ]
        if fixture_ids:
            cmd.extend(["--fixture-id", *fixture_ids])

        env = os.environ.copy()
        env.update(_env_values_for_child())
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "status.showUntrackedFiles"
        env["GIT_CONFIG_VALUE_0"] = "no"

        proc = subprocess.run(
            cmd,
            cwd=str(worktree),
            capture_output=True,
            text=True,
            check=False,
            timeout=3600,
            env=env,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "worktree evaluation failed "
                f"(ref={ref}, commit={resolved}, code={proc.returncode})\n"
                f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )
        if proc.stdout:
            logger.info("worktree eval stdout for %s:\n%s", ref, proc.stdout)
        if proc.stderr:
            logger.warning("worktree eval stderr for %s:\n%s", ref, proc.stderr)

        src_run_dir = worktree / "evaluation" / "runs" / run_id
        dst_run_dir = _RUNS_ROOT / run_id
        _copy_run_outputs(src_run_dir, dst_run_dir)
        _rewrite_copied_paths(dst_run_dir, src_run_dir)
    finally:
        if config_path.exists():
            config_path.unlink()
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


def _copy_run_outputs(src: Path, dst: Path) -> None:
    if not src.exists():
        raise FileNotFoundError(f"worktree run output missing: {src}")
    dst.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        target = dst / child.name
        if child.name == "summary.json":
            continue
        if target.exists():
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)


def _prepare_worktree_excludes(worktree: Path) -> None:
    exclude = worktree / ".git" / "info" / "exclude"
    if not exclude.exists():
        # Detached worktree .git is usually a file pointing to the common git dir.
        git_file = worktree / ".git"
        if git_file.is_file():
            text = git_file.read_text(encoding="utf-8", errors="ignore").strip()
            prefix = "gitdir: "
            if text.startswith(prefix):
                git_dir = Path(text[len(prefix):])
                if not git_dir.is_absolute():
                    git_dir = (worktree / git_dir).resolve()
                exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    patterns = [
        "settings/.env",
        "settings/*.env",
        "gateway/*.env",
        "evaluation/runs/",
        "__pycache__/",
        "**/__pycache__/",
        "*.pyc",
    ]
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    with exclude.open("a", encoding="utf-8") as f:
        for pattern in patterns:
            if pattern not in existing:
                f.write(pattern + "\n")


def _copy_local_env_files(worktree: Path) -> None:
    """Copy ignored local env files needed by older settings loaders."""
    for rel_dir in ("settings", "gateway"):
        src_dir = _ROOT / rel_dir
        if not src_dir.is_dir():
            continue
        dst_dir = worktree / rel_dir
        dst_dir.mkdir(parents=True, exist_ok=True)
        for src in src_dir.iterdir():
            if not src.is_file():
                continue
            if src.name == ".env":
                continue
            if src.name.endswith(".env"):
                shutil.copy2(src, dst_dir / src.name)


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _env_values_for_child() -> dict[str, str]:
    values = _read_env_file(_ROOT / "settings" / ".env")
    env_name = values.get("ENV") or values.get("env") or os.environ.get("ENV") or "local_multi_process"
    values.update(_read_env_file(_ROOT / "settings" / f"worker_{env_name}.env"))
    values.setdefault("ENV", env_name)
    return values


def _rewrite_copied_paths(dst_run_dir: Path, src_run_dir: Path) -> None:
    """Rewrite temp worktree run paths inside copied JSON artifacts."""
    sources = {str(src_run_dir), str(src_run_dir.resolve())}
    dst = str(dst_run_dir)

    def replace(value):
        if isinstance(value, str):
            out = value
            for src in sources:
                out = out.replace(src, dst)
            return out
        if isinstance(value, list):
            return [replace(v) for v in value]
        if isinstance(value, dict):
            return {k: replace(v) for k, v in value.items()}
        return value

    for json_path in dst_run_dir.rglob("*.json"):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rewritten = replace(data)
        if rewritten != data:
            json_path.write_text(
                json.dumps(rewritten, indent=2, default=str),
                encoding="utf-8",
            )


def _merge_summary(run_dir: Path) -> None:
    records = []
    for record_path in sorted(run_dir.glob("*/*/record.json")):
        records.append(json.loads(record_path.read_text(encoding="utf-8")))
    (run_dir / "summary.json").write_text(
        json.dumps(records, indent=2, default=str),
        encoding="utf-8",
    )
