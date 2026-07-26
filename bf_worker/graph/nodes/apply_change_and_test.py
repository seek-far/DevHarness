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
import time
from pathlib import Path

from enhancements.hooks import HookName
from graph.state import BugFixState
from typing import Optional
from langchain_core.runnables import RunnableConfig
from services.apply_patch import PatchAnchorError, apply_change_infos
from services.patch_guard import PatchScopeError, validate_patch_scope
from services.runtime_context import get_budget, get_hooks, get_provider

logger = logging.getLogger(__name__)


def ensure_venv_gitignored(repo_path: Path) -> bool:
    """Keep the venv this node creates out of the fix's output. Returns whether
    a .gitignore entry was written.

    Only meaningful in a git repo — the entire point is to stop `git add -A`
    from staging `.venv/`. In **no-git mode there is no `git add`**, and writing
    the file instead INVENTS a change the user never made: the source dir has no
    .gitignore, so the differ sees a new file and emits it in the user's patch.
    Guarding on `.git/` keeps patches to real source changes only.
    (Found 2026-07-14 alongside the .venv-in-patch leak; the no-git patch walk
    prunes generated *directories*, but .gitignore is a file it can't tell from
    a genuine edit — so the right fix is not to create it.)
    """
    if not (repo_path / ".git").exists():
        return False
    gitignore = repo_path / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if ".venv" in existing:
        return False
    with gitignore.open("a", encoding="utf-8") as f:
        f.write("\n.venv/\n")
    return True


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
    # Phase-3 sub-marker. Fires on every exit path (apply rejection, patch
    # anchor error, pytest run completed, …) so the apply_test_start →
    # apply_test_end pair brackets the whole node body regardless of which
    # branch was taken. `attempt` = the fix_retry_count this node entered
    # with — pairs uniquely with the matching apply_test_start under
    # multi-attempt fixes. `apply_error_present` lets the analyzer separate
    # "fast reject" attempts from "ran pytest" attempts.
    _t0 = state.get("_apply_test_t0_ms")
    if _t0 is not None:
        _t_end = time.time_ns() // 1_000_000
        logger.info(
            "phase_marker phase=apply_test_end bug_id=%s attempt=%d "
            "test_passed=%s apply_error_present=%s "
            "elapsed_ms=%d t_wall_ms=%d",
            state.get("bug_id", ""), int(state.get("fix_retry_count") or 0),
            bool(result.get("test_passed")),
            bool(result.get("apply_error")),
            _t_end - _t0, _t_end,
        )
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


def _apply_model_patch_ver99(state: BugFixState, repo_path: Path) -> dict:
    """workflow_ver == 99: git-apply the mini-produced unified diff to the clone
    and SKIP local pytest (GitLab CI in the eval image is the oracle).

    The clone is already on the auto/bf branch at base_commit (create_fix_branch
    ran first) and mini's diff is relative to base_commit, so the contexts line
    up. Tries `git apply --3way` → `git apply` → `patch -p1` for robustness. On
    success test_passed=True (no local test run); on failure the run routes to
    handle_failure (route_after_apply_and_test handles the ver==99 no-retry rule).
    """
    patch = (state.get("model_patch") or "")
    if not patch.strip():
        return {"test_passed": False, "apply_error": "empty model_patch",
                "test_output": "[ver99] no diff to apply", "error": "ver99: empty model_patch"}
    if not patch.endswith("\n"):
        patch += "\n"   # git apply rejects a diff without a trailing newline

    # Write outside the repo tree so a later `git add -A` in commit_change can't
    # stage the patch file itself.
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False,
                                     encoding="utf-8") as tf:
        tf.write(patch)
        patch_file = tf.name

    last = ""
    try:
        for cmd in (
            ["git", "apply", "--3way", patch_file],
            ["git", "apply", patch_file],
            ["patch", "-p1", "-i", patch_file],
        ):
            proc = subprocess.run(cmd, cwd=str(repo_path), capture_output=True, text=True)
            if proc.returncode == 0:
                logger.info("[ver99] model_patch applied via '%s'; local test skipped (CI is oracle)",
                            " ".join(cmd[:2]))
                return {
                    "test_passed": True,
                    "apply_error": None,
                    "test_output": f"[ver99] model_patch applied via {' '.join(cmd[:2])}; "
                                   f"local pytest skipped — GitLab CI is the test oracle.",
                }
            last = (proc.stderr or proc.stdout or "").strip()
        logger.warning("[ver99] model_patch did not apply to clone: %s", last[-400:])
        return {
            "test_passed": False,
            "apply_error": f"git apply of model_patch failed: {last[-800:]}",
            "test_output": f"[ver99] could not apply model_patch:\n{last[-800:]}",
            "error": "ver99: model_patch did not apply to the clone",
        }
    finally:
        Path(patch_file).unlink(missing_ok=True)


def apply_change_and_test(state: BugFixState, config: Optional[RunnableConfig] = None) -> BugFixState:
    provider = get_provider(config)
    bug_id = state["bug_id"]

    # Phase-3 sub-marker. Brackets the node body via _finalize (every exit
    # path goes through there). Stashing the wallclock-ms start on state
    # under an `_`-prefixed key keeps it out of the checkpointed delta —
    # _finalize strips `_`-prefixed keys before returning.
    _apply_test_t0_ms = time.time_ns() // 1_000_000
    state = {**state, "_apply_test_t0_ms": _apply_test_t0_ms}
    logger.info(
        "phase_marker phase=apply_test_start bug_id=%s attempt=%d t_wall_ms=%d",
        bug_id, int(state.get("fix_retry_count") or 0), _apply_test_t0_ms,
    )

    # Resolve repo path — provider.ensure_repo_ready was already called in
    # create_fix_branch, so we reconstruct the path the same way.
    repo_path = provider.ensure_repo_ready(bug_id)

    # workflow_ver == 99 (SWE-bench substrate): the "fix" is a unified diff mini
    # produced in the docker container, carried on state["model_patch"]. We
    # git-apply it to the clone (already on the auto/bf branch at base_commit)
    # and SKIP the local venv + pytest entirely — GitLab CI in the eval image
    # is the test oracle for this path. Branches out before touching
    # llm_result["fixes"] (which ver==99 never populates).
    if int(state.get("workflow_ver") or 0) == 99:
        return _finalize(state, config, _apply_model_patch_ver99(state, repo_path))

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
    except PatchAnchorError as exc:
        # Anchor mismatch: original_line is not the verbatim current line.
        # Bounded reject (advances fix_retry_count like patch_guard) so the
        # LLM gets actionable feedback and the run still terminates at
        # MAX_FIX_RETRIES instead of silently corrupting the file.
        logger.warning("apply_patch anchor rejected fix: %s", exc)
        return _finalize(state, config, {
            "apply_error": f"patch anchor failed: {exc}",
            "test_passed": False,
            "test_output": f"[apply anchor rejected]\n{exc}",
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

    ensure_venv_gitignored(repo_path)

    req_file = repo_path / "requirements.txt"
    if req_file.exists():
        logger.info("installing dependencies from %s", req_file)
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "-r", str(req_file), "-q"],
            check=True,
        )

    # Phase-3 sub-marker. Splits the apply_change_and_test interval into
    # "setup" (venv create + pip install — usually the giant chunk,
    # especially on the first attempt where pip has to fetch wheels) and
    # "pytest" (apply_test_venv_done → apply_test_end). `had_requirements`
    # flags fixtures that triggered a pip install vs ones that ran pytest
    # against the vanilla venv, so the analyzer can keep those two
    # populations apart when reporting the venv stage cost.
    _venv_done_ms = time.time_ns() // 1_000_000
    logger.info(
        "phase_marker phase=apply_test_venv_done bug_id=%s attempt=%d "
        "had_requirements=%s t_wall_ms=%d",
        bug_id, int(state.get("fix_retry_count") or 0),
        bool(req_file.exists()), _venv_done_ms,
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
