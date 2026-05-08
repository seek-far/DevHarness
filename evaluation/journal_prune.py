"""
journal_prune — retention policy for evaluation/journal/.

Running-mode writes one directory per `agent.fix()` to `evaluation/journal/`.
Without retention these accumulate forever. This module supplies the policy
(planning) and the executor; the `bench journal-prune` CLI wires it up.

Design choices:

- "older than" is computed from the timestamp prefix of the directory name
  (`YYYYMMDDTHHMMSSZ_...`), not from `mtime`. Names are immutable; mtime is
  trivially perturbed by `cp`, `rsync`, or in-place edits.
- Only directories matching the journal naming pattern are eligible for
  deletion. Anything else (notes, hand-placed files, foreign subdirs) is
  preserved. This is a hard safety guarantee: the prune never touches files
  that don't look like journal entries.
- `plan_prune` is a pure function (no IO besides `iterdir`) so it is trivially
  unit-testable. `run_prune` does the `rmtree` and is the only side-effecting
  entry point.
- Default execution is dry-run; the CLI requires `--apply` to actually delete.
"""

from __future__ import annotations
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Journal directories are named: <YYYYMMDDTHHMMSSZ>_<bug_id>_<agent>[_<model_slug>]
# (see bf_worker/journal.py). The leading timestamp is what we key off.
_JOURNAL_DIR_RE = re.compile(r"^(\d{8}T\d{6}Z)_")

_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_DURATION_FACTOR_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(s: str) -> timedelta:
    """Parse '30d' / '12h' / '90m' / '60s' into a timedelta.

    The grammar is intentionally narrow — no compound forms ('1d12h'), no
    floats. Anything off-spec raises ValueError so the CLI surfaces it as an
    obvious error instead of silently treating typos as zero.
    """
    if not s:
        raise ValueError("empty duration")
    m = _DURATION_RE.match(s)
    if not m:
        raise ValueError(
            f"invalid duration: {s!r} (expected e.g. '30d', '12h', '90m', '60s')"
        )
    n = int(m.group(1))
    unit = m.group(2)
    return timedelta(seconds=n * _DURATION_FACTOR_S[unit])


def _entry_timestamp(name: str) -> datetime | None:
    """Extract the UTC timestamp encoded in a journal directory name.

    Returns None when the name doesn't match the journal pattern — caller must
    treat that as "leave alone".
    """
    m = _JOURNAL_DIR_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


@dataclass
class PrunePlan:
    to_delete: list[Path]
    to_keep:   list[tuple[Path, str]]   # (path, reason)


def plan_prune(
    journal_dir: Path,
    *,
    older_than: timedelta,
    keep_flagged: bool,
    now: datetime | None = None,
) -> PrunePlan:
    """Decide what to delete vs keep. Pure function, no rmtree.

    Reasons we keep an entry:
      - "name unrecognized": doesn't look like a journal directory at all.
      - "within window": its timestamp is newer than (now - older_than).
      - "FLAGGED": kept because keep_flagged=True and the FLAGGED marker exists.
    """
    cutoff = (now or datetime.now(timezone.utc)) - older_than
    to_delete: list[Path] = []
    to_keep:   list[tuple[Path, str]] = []

    if not journal_dir.is_dir():
        return PrunePlan(to_delete=[], to_keep=[])

    for entry in sorted(journal_dir.iterdir()):
        if not entry.is_dir():
            # Files at the top level (e.g. a stray .DS_Store or a hand-placed
            # README) are out of scope — never touched.
            continue
        ts = _entry_timestamp(entry.name)
        if ts is None:
            to_keep.append((entry, "name unrecognized"))
            continue
        if ts >= cutoff:
            to_keep.append((entry, "within window"))
            continue
        if keep_flagged and (entry / "FLAGGED").exists():
            to_keep.append((entry, "FLAGGED"))
            continue
        to_delete.append(entry)

    return PrunePlan(to_delete=to_delete, to_keep=to_keep)


def run_prune(
    journal_dir: Path,
    *,
    older_than: timedelta,
    keep_flagged: bool,
    apply: bool,
    now: datetime | None = None,
) -> dict:
    """Plan + (optionally) execute a prune.

    Returns a summary dict the CLI prints. When apply=False, no rmtree is
    issued; only the plan is reported. When apply=True, rmtree errors are
    captured per-entry and surfaced in the summary so a single bad permission
    doesn't abort the whole sweep.
    """
    plan = plan_prune(
        journal_dir,
        older_than=older_than,
        keep_flagged=keep_flagged,
        now=now,
    )
    deleted: list[str] = []
    errors: list[tuple[str, str]] = []
    if apply:
        for entry in plan.to_delete:
            try:
                shutil.rmtree(entry)
                deleted.append(entry.name)
            except OSError as exc:
                errors.append((entry.name, str(exc)))
                logger.warning("journal-prune: failed to remove %s: %s", entry, exc)

    return {
        "journal_dir":  str(journal_dir),
        "older_than_s": older_than.total_seconds(),
        "keep_flagged": keep_flagged,
        "dry_run":      not apply,
        "candidates":   [p.name for p in plan.to_delete],
        "kept":         [(p.name, why) for p, why in plan.to_keep],
        "deleted":      deleted,
        "errors":       errors,
    }
