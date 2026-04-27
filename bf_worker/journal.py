"""
JournalWriter — captures every running-mode run for retrospective evaluation curation.

Every invocation of an Agent in running mode writes one directory under
{journal_dir}/{ts}_{bug_id}_{agent}/ containing:
  - record.json:   structured RunRecord (config, outcome, iterations, error)
  - trace.txt:     captured failure trace (if present)
  - test_output.txt: pytest output from the last apply_change_and_test step
  - llm_result.json: the LLM's final structured fix proposal
  - FLAGGED:       sentinel file for runs worth promoting to a fixture
                   (failed runs, no_fix, high-iteration successes)

The journal is intentionally cheap and always-on: we don't know a bug is
"interesting" until after it plays out. Curation (promote candidates to
benchmark fixtures) happens later via `evaluation/cli.py`.

Override the destination via env var BF_JOURNAL_DIR.
"""

from __future__ import annotations
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# bf_worker/journal.py → project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DIR = _PROJECT_ROOT / "evaluation" / "journal"


class JournalWriter:
    def __init__(self, journal_dir: str | Path | None = None):
        env_override = os.environ.get("BF_JOURNAL_DIR")
        if journal_dir is not None:
            self.journal_dir = Path(journal_dir)
        elif env_override:
            self.journal_dir = Path(env_override)
        else:
            self.journal_dir = _DEFAULT_DIR
        self.journal_dir.mkdir(parents=True, exist_ok=True)

    def write(self, agent_name: str, bug_input, fix_output, raw_state: dict | None) -> Path | None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = self.journal_dir / f"{ts}_{bug_input.bug_id}_{agent_name}"
        try:
            run_dir.mkdir(parents=True, exist_ok=True)

            record = {
                "agent":           agent_name,
                "bug_id":          bug_input.bug_id,
                "project_id":      bug_input.project_id,
                "project_web_url": bug_input.project_web_url,
                "job_id":          bug_input.job_id,
                "outcome":         fix_output.outcome,
                "error":           fix_output.error,
                "iterations":      fix_output.iterations,
                "timestamp":       ts,
            }
            (run_dir / "record.json").write_text(
                json.dumps(record, indent=2, default=str), encoding="utf-8"
            )

            state = fix_output.final_state or {}
            if state.get("trace"):
                (run_dir / "trace.txt").write_text(str(state["trace"]), encoding="utf-8")
            if state.get("test_output"):
                (run_dir / "test_output.txt").write_text(str(state["test_output"]), encoding="utf-8")
            if state.get("llm_result") is not None:
                (run_dir / "llm_result.json").write_text(
                    json.dumps(state["llm_result"], indent=2, default=str),
                    encoding="utf-8",
                )

            flag = _flag_reason(fix_output)
            if flag:
                (run_dir / "FLAGGED").write_text(flag + "\n", encoding="utf-8")

            logger.info("journal entry: %s", run_dir)
            return run_dir
        except Exception as exc:
            logger.warning("journal write failed: %s", exc)
            return None


def _flag_reason(fix_output) -> str | None:
    """Heuristic: which runs are worth surfacing for fixture-promotion review."""
    if fix_output.outcome != "fixed":
        return f"outcome={fix_output.outcome}"
    if fix_output.iterations >= 2:
        return f"high_iterations={fix_output.iterations}"
    return None
