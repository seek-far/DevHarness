"""
Node: apply_change_and_test

1. Apply the LLM-suggested line changes to the source file on disk.
2. Run pytest inside the repo.
3. Write test_passed + test_output (+ apply_error if apply itself crashed)
   into state so the router can decide the next node.
"""

from __future__ import annotations
import logging
import subprocess
import sys
from pathlib import Path

from enhancements.hooks import HookName
from graph.state import BugFixState
from typing import Optional
from langchain_core.runnables import RunnableConfig
from services.apply_patch import apply_change_infos
from services.patch_guard import PatchScopeError, validate_patch_scope
from services.runtime_context import get_budget, get_hooks, get_provider

logger = logging.getLogger(__name__)


def _finalize(
    state: BugFixState,
    config: Optional[RunnableConfig],
    result: dict,
) -> dict:
    """Fire the POST_APPLY_TEST hook on failure, fold its output into `result`.

    On a passing run this is a no-op (a green run must never pay the cost of
    a post-mortem). On any failure path it runs the hook against a merged
    view of state+result so callbacks (e.g. the reflection enhancement) see
    the patch, apply_error and test_output that just failed.

    `_budget` is passed transiently so a callback can account its own LLM
    call against the run budget; it is stripped from the persisted delta so
    the non-serializable RunBudget never enters checkpointed state. Only keys
    a callback actually added/changed are returned, keeping this node generic
    (no enhancement-specific keys hard-coded here).
    """
    if result.get("test_passed"):
        return result
    hooks = get_hooks(config)
    if hooks is None or not hooks.has(HookName.POST_APPLY_TEST):
        return result
    before = {**state, **result}
    after = hooks.run(HookName.POST_APPLY_TEST, {**before, "_budget": get_budget(config)})
    delta = {
        k: v
        for k, v in after.items()
        if not k.startswith("_") and (k not in before or before.get(k) != v)
    }
    return {**result, **delta}


def apply_change_and_test(state: BugFixState, config: Optional[RunnableConfig] = None) -> BugFixState:
    provider = get_provider(config)
    bug_id = state["bug_id"]

    # Resolve repo path — provider.ensure_repo_ready was already called in
    # create_fix_branch, so we reconstruct the path the same way.
    repo_path = provider.ensure_repo_ready(bug_id)

    llm_result = state["llm_result"]
    change_infos = llm_result["fixes"]
    suspect_file = state.get("suspect_file_path") or ""
    source_fetch_failed = bool(state.get("source_fetch_failed"))

    # Group fixes by target file. A fix entry's `file_path` (if present) wins;
    # otherwise the suspect file is used. This lets the LLM fix an imported
    # module when the suspect happens to be a test file.
    #
    # The suspect_file fallback is only safe when we actually have content for
    # that suspect — i.e. parse_trace_fallback is False AND source_fetch_failed
    # is False. In either fallback mode, a fix that omits `file_path` would
    # resolve to either an empty string (corrupting apply) or a path we know
    # is unreadable. Reject via the existing apply_error → retry channel so
    # the LLM sees the error on the next loop turn and can revise.
    fixes_by_file: dict[str, list[dict]] = {}
    for f in change_infos:
        explicit = f.get("file_path")
        if explicit:
            target = explicit
        elif suspect_file and not source_fetch_failed:
            target = suspect_file
        else:
            err = (
                "fix entry is missing required `file_path`. "
                + (
                    "No suspect file was pre-identified, "
                    if not suspect_file
                    else f"Suspect file `{suspect_file}` could not be read, "
                )
                + "so every fix MUST set `file_path` explicitly to a path "
                "within the repo."
            )
            logger.warning("apply_change_and_test rejected fix: %s", err)
            return _finalize(state, config, {
                "apply_error": err,
                "test_passed": False,
                "test_output": f"[apply rejected]\n{err}",
                "fix_retry_count": state.get("fix_retry_count", 0) + 1,
            })
        fixes_by_file.setdefault(target, []).append(f)

    # ── 1. Apply patch ───────────────────────────────────────────────���────────
    try:
        validate_patch_scope(repo_path, fixes_by_file)
        for rel_path, fixes in fixes_by_file.items():
            src_filepath = str(repo_path / rel_path)
            apply_change_infos(src_filepath=src_filepath, change_infos=fixes)
            logger.info("patch applied to %s (%d edits)", src_filepath, len(fixes))
    except PatchScopeError as exc:
        logger.warning("patch_guard rejected fix: %s", exc)
        return _finalize(state, config, {
            "apply_error": f"patch rejected by guardrail: {exc}",
            "test_passed": False,
            "test_output": f"[patch_guard rejected]\n{exc}",
            "fix_retry_count": state.get("fix_retry_count", 0) + 1,
        })
    except Exception as exc:
        logger.warning("apply_patch failed: %s", exc)
        return _finalize(state, config, {
            "apply_error": str(exc),
            "test_passed": False,
            "test_output": f"[apply_patch error]\n{exc}",
        })

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

    # ── 3. Run pytest ──────────���────────────────────────────────���─────────────
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
    return _finalize(state, config, result)
