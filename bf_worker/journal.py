"""
JournalWriter — captures every running-mode run for retrospective evaluation curation.

Every invocation of an Agent in running mode writes one directory under
{journal_dir}/{ts}_{bug_id}_{agent}/ containing:
  - record.json:   serialized RunRecord (canonical schema, see agents.run_record)
  - trace.txt:     captured failure trace (if present)
  - test_output.txt: pytest output from the last apply_change_and_test step
  - llm_result.json: the LLM's final structured fix proposal
  - FLAGGED:       sentinel file for runs worth promoting to a fixture
                   (failed runs, no_fix, high-iteration successes)

The journal is intentionally cheap and always-on: we don't know a bug is
"interesting" until after it plays out. Curation (promote candidates to
benchmark fixtures) happens later via `evaluation/cli.py promote`.

Override the destination via env var BF_JOURNAL_DIR.
"""

from __future__ import annotations
import json
import logging
import os
import re
from pathlib import Path

from agents.run_record import RunRecord

logger = logging.getLogger(__name__)

# bf_worker/journal.py → project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DIR = _PROJECT_ROOT / "evaluation" / "journal"

# Anything outside this set is replaced with `-` to keep directory names
# filesystem-safe (slashes in vendor-prefixed model IDs like `mistralai/...`
# would otherwise create a stray subdirectory).
_MODEL_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]")
_MODEL_SLUG_MAX = 60


def _model_slug(model: str | None) -> str:
    """Return a filesystem-safe slug for the model name, or '' if absent."""
    if not model:
        return ""
    slug = _MODEL_SLUG_RE.sub("-", model).strip("-")
    return slug[:_MODEL_SLUG_MAX]


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

    def write(self, record: RunRecord, final_state: dict | None) -> Path | None:
        dir_name = f"{record.timestamp}_{record.bug_id}_{record.agent_name}"
        slug = _model_slug(record.llm_model)
        if slug:
            dir_name = f"{dir_name}_{slug}"
        run_dir = self.journal_dir / dir_name
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "record.json").write_text(record.to_json(), encoding="utf-8")

            state = final_state or {}
            if state.get("trace"):
                (run_dir / "trace.txt").write_text(str(state["trace"]), encoding="utf-8")
            if state.get("test_output"):
                (run_dir / "test_output.txt").write_text(str(state["test_output"]), encoding="utf-8")
            if state.get("llm_result") is not None:
                (run_dir / "llm_result.json").write_text(
                    json.dumps(state["llm_result"], indent=2, default=str),
                    encoding="utf-8",
                )
            if state.get("budget") is not None:
                (run_dir / "budget.json").write_text(
                    json.dumps(state["budget"], indent=2, default=str),
                    encoding="utf-8",
                )

            flag = _flag_reason(record)
            if flag:
                (run_dir / "FLAGGED").write_text(flag + "\n", encoding="utf-8")

            logger.info("journal entry: %s", run_dir)
            return run_dir
        except Exception as exc:
            logger.warning("journal write failed: %s", exc)
            return None


def _flag_reason(record: RunRecord) -> str | None:
    """Heuristic: which runs are worth surfacing for fixture-promotion review."""
    if record.outcome != "fixed":
        return f"outcome={record.outcome}"
    if record.iterations >= 2:
        return f"high_iterations={record.iterations}"
    return None
