"""
Local providers — run bug-fix workflow against a local directory,
independent of GitLab or any remote CI system.

Two modes:
  - LocalGitProvider:   source dir is a git repo, uses git for branching/commit
  - LocalNoGitProvider: plain directory, works on a temp copy, outputs a patch file
"""

from __future__ import annotations
import datetime
import difflib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .base import SourceProvider, VCSProvider, ReviewProvider

logger = logging.getLogger(__name__)


# ── Shared helpers ────────────────────────────────────────────────────────────

_VENV_EXCLUDE_PATTERNS = {".venv", ".venv/", "/.venv", "/.venv/"}


def _ensure_venv_excluded_from_git(work_dir: Path) -> None:
    """
    For git repos, append `.venv/` to `.git/info/exclude` so the venv created
    by `_ensure_venv` is not staged by `git add -A` during commit.

    Uses `.git/info/exclude` (local, untracked) rather than `.gitignore`
    (tracked) to avoid mutating the user's repo or polluting the fix commit.
    No-op when work_dir is not a git repo (e.g. LocalNoGitProvider's temp copy).
    """
    git_dir = work_dir / ".git"
    if not git_dir.is_dir():
        return

    exclude_file = git_dir / "info" / "exclude"
    existing = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
    for line in existing.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped in _VENV_EXCLUDE_PATTERNS:
            return

    exclude_file.parent.mkdir(parents=True, exist_ok=True)
    prefix = "" if (not existing or existing.endswith("\n")) else "\n"
    with exclude_file.open("a", encoding="utf-8") as f:
        f.write(f"{prefix}.venv/\n")
    logger.info("added .venv/ to %s (local-only, not committed)", exclude_file)


def _ensure_venv(work_dir: Path) -> Path | None:
    """
    Create `.venv` in `work_dir` (idempotent) and install `requirements.txt`
    if present. Returns the venv's bin/Scripts directory, or None if no venv
    was needed (no requirements.txt and no existing .venv).
    """
    venv_path = work_dir / ".venv"
    bin_dir = venv_path / ("Scripts" if sys.platform == "win32" else "bin")
    venv_python = bin_dir / ("python.exe" if sys.platform == "win32" else "python")
    req_file = work_dir / "requirements.txt"

    if venv_python.exists():
        _ensure_venv_excluded_from_git(work_dir)
        logger.debug("venv already exists at %s", venv_path)
        return bin_dir

    if not req_file.exists():
        logger.debug("no requirements.txt at %s, skipping venv creation", work_dir)
        return None

    _ensure_venv_excluded_from_git(work_dir)
    logger.info("creating venv at %s", venv_path)
    subprocess.run(["python", "-m", "venv", str(venv_path)], check=True)

    logger.info("installing dependencies from %s", req_file)
    subprocess.run(
        [str(venv_python), "-m", "pip", "install", "-r", str(req_file), "-q"],
        check=True,
    )
    return bin_dir


def _run_test_cmd(repo_path: Path, test_cmd: str) -> tuple[bool, str]:
    """
    Run a test command and return (passed, output).
    If `repo_path/.venv/` exists (or requirements.txt is present so we create one),
    prepend the venv's bin dir to PATH so the command resolves to venv-installed tools.
    """
    bin_dir = _ensure_venv(repo_path)
    env = os.environ.copy()
    if bin_dir is not None:
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env["VIRTUAL_ENV"] = str(repo_path / ".venv")
        logger.info("running test command with venv: %s in %s", test_cmd, repo_path)
    else:
        logger.info("running test command: %s in %s", test_cmd, repo_path)
    proc = subprocess.run(
        test_cmd,
        shell=True,
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        env=env,
    )
    output = proc.stdout + proc.stderr
    return proc.returncode == 0, output


def _read_local_file(source_dir: Path, file_path: str) -> str:
    full_path = source_dir / file_path
    if not full_path.exists():
        raise FileNotFoundError(f"File not found: {full_path}")
    return full_path.read_text(encoding="utf-8", errors="ignore")


# ── LocalGitProvider ──────────────────────────────────────────────────────────

class LocalGitProvider(SourceProvider, VCSProvider, ReviewProvider):
    """Local git repo mode — branches, commits locally, no remote push."""

    def __init__(self, source_dir: str, trace_file: str = "",
                 test_cmd: str = "pytest"):
        self._source_dir = Path(source_dir).resolve()
        self._trace_file = trace_file
        self._test_cmd = test_cmd

        if not (self._source_dir / ".git").exists():
            raise ValueError(f"Not a git repo: {self._source_dir}")

    def _git(self, *args, cwd=None):
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or str(self._source_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Git error: {result.stderr.strip()}")
        return result.stdout.strip()

    # ── SourceProvider ────────────────────────────────────────────────────────

    def fetch_trace(self, **kwargs) -> str:
        if self._trace_file:
            logger.info("reading trace from file: %s", self._trace_file)
            return Path(self._trace_file).read_text(encoding="utf-8", errors="ignore")
        # Run test command and capture output as trace
        logger.info("running test command to generate trace: %s", self._test_cmd)
        _, output = _run_test_cmd(self._source_dir, self._test_cmd)
        return output

    def fetch_file(self, file_path: str, ref: str = "main") -> str:
        return _read_local_file(self._source_dir, file_path)

    # ── VCSProvider ───────────────────────────────────────────────────────────

    def ensure_repo_ready(self, bug_id: str) -> Path:
        # Repo already exists locally; just return the path
        logger.info("local git repo ready at %s", self._source_dir)
        return self._source_dir

    def create_fix_branch(self, bug_id: str, repo_path: Path) -> dict:
        """Idempotent: deterministic branch name keyed on (bug_id, base_commit).

        No remote concept here — there's no MR to look up, so `existing_mr` is
        never returned and R10 short-circuit doesn't apply to LocalGit.
        """
        base_commit = self._git("rev-parse", "HEAD", cwd=str(repo_path))
        branch_name = f"auto/bf/{bug_id}-{base_commit[:8]}"

        local_exists = self._branch_exists(branch_name, repo_path)
        if local_exists:
            self._git("checkout", branch_name, cwd=str(repo_path))
            status = "reused"
            logger.info("reused local fix branch: %s", branch_name)
        else:
            self._git("checkout", "-b", branch_name, cwd=str(repo_path))
            status = "success"
            logger.info("created local fix branch: %s", branch_name)

        return {
            "status":      status,
            "branch_name": branch_name,
            "base_branch": "HEAD",
            "commit":      base_commit,
        }

    def _branch_exists(self, branch_name: str, repo_path: Path) -> bool:
        try:
            self._git("rev-parse", "--verify", "--quiet",
                      f"refs/heads/{branch_name}", cwd=str(repo_path))
            return True
        except RuntimeError:
            return False

    def commit_and_push(self, repo_path: Path, message: str) -> dict:
        """Three-state commit (no push — local mode).

          no working-tree changes        → status="no_changes"
          fresh commit on top of base    → status="success"
          existing commit, same tree     → status="reused" (no-op, no new commit)
          existing commit, diff tree     → status="updated" (reset + commit afresh)

        For LocalGit we have no remote, so "force-push" maps to
        `git reset --hard` followed by re-commit. This keeps the branch's HEAD
        pointing at exactly one commit ahead of base — same invariant the
        GitLab path enforces via force-push.
        """
        status = self._git("status", "--porcelain", cwd=str(repo_path))
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD", cwd=str(repo_path))

        # Identify base = first parent of branch HEAD if there's a commit ahead, else HEAD itself.
        try:
            base_commit = self._git("rev-parse", f"{branch}~1", cwd=str(repo_path))
            has_prior_commit = True
        except RuntimeError:
            base_commit = self._git("rev-parse", "HEAD", cwd=str(repo_path))
            has_prior_commit = False

        if has_prior_commit and not status:
            # No new working-tree changes; check whether the existing commit's
            # tree already matches what we'd have committed (R5b / R6 reuse).
            current_tree = self._git("rev-parse", "HEAD^{tree}", cwd=str(repo_path))
            commit_hash = self._git("rev-parse", "HEAD", cwd=str(repo_path))
            logger.info("existing commit on branch, no working-tree changes — reused")
            return {"status": "reused", "branch": branch, "commit": commit_hash,
                    "tree": current_tree}

        if not status and not has_prior_commit:
            return {"status": "no_changes"}

        if has_prior_commit:
            # Existing commit + new working-tree changes → reset to base, then commit fresh.
            # This is the local-mode analog of force-push.
            logger.info("stale commit on branch, resetting to base %s and re-committing",
                        base_commit[:8])
            self._git("reset", "--hard", base_commit, cwd=str(repo_path))

        self._git("add", "-A", cwd=str(repo_path))
        self._git("commit", "-m", message, cwd=str(repo_path))
        commit_hash = self._git("rev-parse", "HEAD", cwd=str(repo_path))

        push_status = "updated" if has_prior_commit else "success"
        logger.info("committed locally: %s on %s (status=%s)",
                    commit_hash[:8], branch, push_status)
        return {"status": push_status, "branch": branch, "commit": commit_hash}

    # ── ReviewProvider ────────────────────────────────────────────────────────

    def create_review(self, repo_path: Path, state: dict) -> dict:
        logger.info("local git mode: fix committed on branch %s", state.get("fix_branch_name"))
        return {"status": "local_commit", "branch": state.get("fix_branch_name")}

    def wait_ci_result(self, bug_id: str, timeout: int = 300) -> str | None:
        # In local mode, tests were already run in apply_change_and_test.
        # Return success to let the graph proceed to create_review.
        logger.info("local mode: skipping CI wait, using local test result")
        return "success"


# ── LocalNoGitProvider ────────────────────────────────────────────────────────

class LocalNoGitProvider(SourceProvider, VCSProvider, ReviewProvider):
    """
    Plain directory mode — no git required.
    Creates a temp copy of the source dir to work in.
    Produces a patch file and report for user review.
    """

    def __init__(self, source_dir: str, output_dir: str = ".",
                 trace_file: str = "", test_cmd: str = "pytest",
                 bug_id: str = "BUG-LOCAL"):
        self._source_dir = Path(source_dir).resolve()
        self._output_dir = Path(output_dir).resolve()
        self._trace_file = trace_file
        self._test_cmd = test_cmd
        self._bug_id = bug_id
        self._work_dir: Path | None = None  # set during ensure_repo_ready

        if not self._source_dir.is_dir():
            raise ValueError(f"Source directory not found: {self._source_dir}")

    @property
    def work_dir(self) -> Path:
        if self._work_dir is None:
            raise RuntimeError("ensure_repo_ready() must be called first")
        return self._work_dir

    # ── SourceProvider ────────────────────────────────────────────────────────

    def fetch_trace(self, **kwargs) -> str:
        if self._trace_file:
            logger.info("reading trace from file: %s", self._trace_file)
            return Path(self._trace_file).read_text(encoding="utf-8", errors="ignore")
        # Run test command in the temp working copy so .venv doesn't pollute source
        work_dir = self.ensure_repo_ready(self._bug_id)
        logger.info("running test command to generate trace: %s", self._test_cmd)
        _, output = _run_test_cmd(work_dir, self._test_cmd)
        return output

    def fetch_file(self, file_path: str, ref: str = "main") -> str:
        # Read from the working copy if available, else from source
        base = self._work_dir if self._work_dir else self._source_dir
        return _read_local_file(base, file_path)

    # ── VCSProvider ───────────────────────────────────────────────────────────

    def ensure_repo_ready(self, bug_id: str) -> Path:
        # Idempotent: only copy once
        if self._work_dir and self._work_dir.exists():
            return self._work_dir
        tmp_base = Path(tempfile.gettempdir()) / "sdlcma_local" / bug_id
        if tmp_base.exists():
            shutil.rmtree(tmp_base)
        shutil.copytree(self._source_dir, tmp_base)
        self._work_dir = tmp_base
        logger.info("created working copy at %s", tmp_base)
        return tmp_base

    def create_fix_branch(self, bug_id: str, repo_path: Path) -> dict:
        # No-op for no-git mode — just record a label
        branch_name = f"local-fix-{bug_id}"
        logger.info("no-git mode: logical branch label = %s", branch_name)
        return {
            "status": "success",
            "branch_name": branch_name,
            "base_branch": "none",
            "commit": "none",
        }

    def commit_and_push(self, repo_path: Path, message: str) -> dict:
        # Generate a unified diff patch comparing original to modified
        patch_lines = []
        for root, _dirs, files in os.walk(str(repo_path)):
            for fname in files:
                mod_path = Path(root) / fname
                rel = mod_path.relative_to(repo_path)
                orig_path = self._source_dir / rel

                if not orig_path.exists():
                    # New file
                    mod_content = mod_path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
                    patch_lines.extend(difflib.unified_diff(
                        [], mod_content,
                        fromfile=f"a/{rel}", tofile=f"b/{rel}",
                    ))
                    continue

                try:
                    orig_content = orig_path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
                    mod_content = mod_path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
                except (UnicodeDecodeError, PermissionError):
                    continue

                if orig_content != mod_content:
                    patch_lines.extend(difflib.unified_diff(
                        orig_content, mod_content,
                        fromfile=f"a/{rel}", tofile=f"b/{rel}",
                    ))

        if not patch_lines:
            logger.info("no changes detected")
            return {"status": "no_changes"}

        # Write patch file
        self._output_dir.mkdir(parents=True, exist_ok=True)
        patch_path = self._output_dir / f"{self._bug_id}.patch"
        patch_path.write_text("".join(patch_lines), encoding="utf-8")
        logger.info("patch written to %s", patch_path)

        return {"status": "success", "patch_file": str(patch_path)}

    # ── ReviewProvider ────────────────────────────────────────────────────────

    def create_review(self, repo_path: Path, state: dict) -> dict:
        bug_id = state.get("bug_id", "unknown")
        self._output_dir.mkdir(parents=True, exist_ok=True)

        patch_path = self._output_dir / f"{bug_id}.patch"
        report_path = self._output_dir / f"{bug_id}_report.md"

        # Build report
        lines = [
            f"# Bug Fix Report: {bug_id}\n",
            f"\n## Error Summary\n\n```\n{state.get('error_info', 'N/A')}\n```\n",
            f"\n## Suspect File\n\n`{state.get('suspect_file_path', 'N/A')}`\n",
            f"\n## Test Result\n\n**Passed:** {state.get('test_passed', 'N/A')}\n",
        ]

        if state.get("test_output"):
            lines.append(f"\n```\n{state['test_output'][:2000]}\n```\n")

        if state.get("react_reasoning"):
            lines.append(f"\n## LLM Reasoning\n\n{state['react_reasoning']}\n")

        lines.append(f"\n## Apply the Fix\n\n```bash\npatch -p1 -d {self._source_dir} < {patch_path}\n```\n")
        lines.append(f"\n## Working Copy\n\n`{repo_path}`\n")

        report_path.write_text("".join(lines), encoding="utf-8")
        logger.info("report written to %s", report_path)

        # Print summary to console
        print(f"\nFix generated and tested ({'PASSED' if state.get('test_passed') else 'FAILED'}).")
        print(f"\n  Review:  {report_path}")
        if patch_path.exists():
            print(f"  Patch:   {patch_path}")
            print(f"\n  To apply:  patch -p1 -d {self._source_dir} < {patch_path}")
        print()

        return {
            "status": "report_generated",
            "report_file": str(report_path),
            "patch_file": str(patch_path) if patch_path.exists() else None,
        }

    def wait_ci_result(self, bug_id: str, timeout: int = 300) -> str | None:
        # In no-git mode, tests were already run locally.
        logger.info("no-git mode: skipping CI wait, using local test result")
        return "success"
