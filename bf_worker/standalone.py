"""
standalone.py — Run the bug-fix workflow against a local directory,
independent of GitLab, CI pipelines, or Redis.

Usage:

  # With a trace file:
  python -m bf_worker.standalone \
    --source-dir /path/to/project \
    --trace-file /path/to/error.log \
    --bug-id BUG-LOCAL-1

  # Auto-discover errors by running tests:
  python -m bf_worker.standalone \
    --source-dir /path/to/project \
    --test-cmd "pytest tests/" \
    --bug-id BUG-LOCAL-1

  # No-git mode (plain directory, outputs patch file):
  python -m bf_worker.standalone \
    --source-dir /path/to/project \
    --trace-file error.log \
    --no-git \
    --output-dir ./results \
    --bug-id BUG-LOCAL-1

  # Interactive review (pause before finishing):
  python -m bf_worker.standalone \
    --source-dir /path/to/project \
    --trace-file error.log \
    --no-git \
    --review \
    --bug-id BUG-LOCAL-1
"""

from __future__ import annotations
import argparse
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

# Ensure project root and bf_worker/ are both on sys.path.
# - project root: needed for `from settings import ...`
# - bf_worker/:   needed for `from graph...`, `from providers...`, `from services...`
#   (graph nodes use sibling-style imports written for direct script execution)
_HERE = Path(__file__).resolve().parent          # bf_worker/
sys.path.insert(0, str(_HERE.parent))            # project root
sys.path.insert(0, str(_HERE))                   # bf_worker/

from agents.base import BugInput
from agents.langgraph_agent import LangGraphAgent
from journal import JournalWriter
from providers.local_provider import LocalGitProvider, LocalNoGitProvider

logger = logging.getLogger(__name__)


def _has_git(source_dir: Path) -> bool:
    return (source_dir / ".git").exists()


def _interactive_review(source_dir: Path, work_dir: Path) -> bool:
    """Show diff and ask user whether to apply."""
    print("\n--- Side-by-side diff (original vs modified) ---\n")
    try:
        proc = subprocess.run(
            ["diff", "-ru", "--color=always", str(source_dir), str(work_dir)],
            capture_output=True,
            text=True,
        )
        print(proc.stdout or "(no differences)")
    except FileNotFoundError:
        print("(diff command not available, inspect working copy manually)")
        print(f"  Working copy: {work_dir}")

    print()
    while True:
        choice = input("Apply fix to original source? [y]es / [n]o: ").strip().lower()
        if choice in ("y", "yes"):
            return True
        if choice in ("n", "no"):
            return False
        print("Please enter 'y' or 'n'.")


def _apply_patch_to_source(source_dir: Path, patch_file: Path) -> None:
    """Apply a unified diff patch to the source directory."""
    proc = subprocess.run(
        ["patch", "-p1", "-d", str(source_dir), "-i", str(patch_file)],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        print(f"Patch applied successfully to {source_dir}")
    else:
        print(f"Patch application failed:\n{proc.stderr}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run bug-fix workflow locally, independent of GitLab/CI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--source-dir", required=True,
        help="Path to the project source directory.",
    )
    parser.add_argument(
        "--trace-file", default="",
        help="Path to a file containing test/CI output (error trace).",
    )
    parser.add_argument(
        "--test-cmd", default="pytest",
        help="Test command to run if --trace-file is not provided (default: pytest).",
    )
    parser.add_argument(
        "--bug-id", default="BUG-LOCAL-1",
        help="Bug identifier for this fix (default: BUG-LOCAL-1).",
    )
    parser.add_argument(
        "--no-git", action="store_true",
        help="Force no-git mode (auto-detected if source-dir has no .git).",
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="Directory for patch and report output in no-git mode "
             "(default: a fresh temp directory under {tmp}/sdlcma_out/).",
    )
    parser.add_argument(
        "--review", action="store_true",
        help="Interactive review: show diff and ask before applying (no-git mode only).",
    )
    args = parser.parse_args()

    source_dir = Path(args.source_dir).resolve()
    if not source_dir.is_dir():
        print(f"Error: source directory not found: {source_dir}", file=sys.stderr)
        sys.exit(1)

    # Select provider
    use_git = _has_git(source_dir) and not args.no_git

    if use_git:
        logger.info("using LocalGitProvider (source has .git)")
        provider = LocalGitProvider(
            source_dir=str(source_dir),
            trace_file=args.trace_file,
            test_cmd=args.test_cmd,
        )
    else:
        output_dir = args.output_dir
        if output_dir is None:
            tmp_root = Path(tempfile.gettempdir()) / "sdlcma_out"
            tmp_root.mkdir(parents=True, exist_ok=True)
            output_dir = tempfile.mkdtemp(prefix=f"{args.bug_id}_", dir=str(tmp_root))
            logger.info("no --output-dir given, using temp dir: %s", output_dir)
        logger.info("using LocalNoGitProvider (no-git mode)")
        provider = LocalNoGitProvider(
            source_dir=str(source_dir),
            output_dir=output_dir,
            trace_file=args.trace_file,
            test_cmd=args.test_cmd,
            bug_id=args.bug_id,
        )

    # Build agent + bug input
    bug_input = BugInput(bug_id=args.bug_id, provider=provider)
    agent = LangGraphAgent(journal=JournalWriter())

    logger.info("invoking agent=%s ...", agent.name)
    fix_output = agent.fix(bug_input)

    if fix_output.outcome == "error":
        logger.error("agent finished with error: %s", fix_output.error)
        print(f"\nFix failed: {fix_output.error}", file=sys.stderr)
        sys.exit(1)

    logger.info("agent finished: outcome=%s iterations=%d",
                fix_output.outcome, fix_output.iterations)

    # Interactive review for no-git mode
    if args.review and not use_git and hasattr(provider, '_work_dir') and provider._work_dir:
        accepted = _interactive_review(source_dir, provider._work_dir)
        if accepted:
            patch_file = Path(provider._output_dir) / f"{args.bug_id}.patch"
            if patch_file.exists():
                _apply_patch_to_source(source_dir, patch_file)
            else:
                print("No patch file found to apply.")
        else:
            print("Fix rejected. Working copy preserved at:", provider._work_dir)


if __name__ == "__main__":
    _bug_id = "?"
    if "--bug-id" in sys.argv:
        try:
            _bug_id = sys.argv[sys.argv.index("--bug-id") + 1]
        except IndexError:
            pass

    logging.basicConfig(
        level=logging.DEBUG,
        format=f"%(asctime)s %(levelname)s [standalone:{_bug_id} %(name)s:%(funcName)s:%(lineno)d] %(message)s",
        stream=sys.stdout,
        force=True,
    )
    main()
