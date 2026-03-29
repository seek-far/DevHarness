"""
Node: apply_change_and_test

1. Apply the LLM-suggested line changes to the source file on disk.
2. Run pytest inside the repo.
3. Write test_passed + test_output (+ apply_error if apply itself crashed)
   into state so the router can decide the next node.
"""

from __future__ import annotations
import logging
import os
import subprocess
import sys
from pathlib import Path

from graph.state import BugFixState
from services.apply_patch import apply_change_infos

sys.path.append(str(Path.cwd().parent))
from settings import worker_cfg as cfg

logger = logging.getLogger(__name__)


def apply_change_and_test(state: BugFixState) -> BugFixState:
    repo_path = Path(cfg.repo_base_path) / state["bug_id"]
    llm_result = state["llm_result"]
    change_infos = llm_result["fixes"]
    suspect_file = state["suspect_file_path"]
    src_filepath = str(repo_path / suspect_file)

    # ── 1. Apply patch ────────────────────────────────────────────────────────
    try:
        apply_change_infos(src_filepath=src_filepath, change_infos=change_infos)
        logger.info("patch applied to %s", src_filepath)
    except Exception as exc:
        logger.warning("apply_patch failed: %s", exc)
        return {
            "apply_error": str(exc),
            "test_passed": False,
            "test_output": f"[apply_patch error]\n{exc}",
        }

    # ── 2. Create isolated venv and install project dependencies ──────────────
    venv_path = repo_path / ".venv"
    logger.info("creating venv at %s", venv_path)
    subprocess.run(["python", "-m", "venv", str(venv_path)], check=True)
    venv_python = venv_path / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")

    # Ensure .venv is ignored by git so it is never committed
    gitignore = repo_path / ".gitignore"
    gitignore_entry = "\n.venv/\n"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if ".venv" not in existing:
        with gitignore.open("a", encoding="utf-8") as f:
            f.write(gitignore_entry)

    req_file = repo_path / "requirements.txt"
    if req_file.exists():
        logger.info("installing dependencies from %s", req_file)
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "-r", str(req_file), "-q"],
            check=True,
        )

    # ── 3. Run pytest ─────────────────────────────────────────────────────────
    logger.info("running pytest in %s", repo_path)
    proc = subprocess.run(
        [str(venv_python), "-m", "pytest", "--tb=short", "-q"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    test_output = proc.stdout + proc.stderr
    test_passed = proc.returncode == 0

    logger.info("pytest finished: returncode=%d passed=%s", proc.returncode, test_passed)
    logger.debug("pytest output:\n%s", test_output)

    result = {
        "test_passed": test_passed,
        "test_output": test_output,
        "apply_error": None,
    }
    if not test_passed:
        result["fix_retry_count"] = state.get("fix_retry_count", 0) + 1
    return result
