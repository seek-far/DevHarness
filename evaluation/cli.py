"""
bench CLI — manage the evaluation lifecycle.

Subcommands:
    list-fixtures              show all fixtures in evaluation/fixtures/
    list-journal [--flagged]   show journal entries (optionally only auto-flagged ones)
    run    [--agents ...]      sweep agents × fixtures, write evaluation/runs/<run_id>/
    report <run_id>            aggregate metrics for a sweep
    promote <journal_entry>    promote a journal run into a fixture (curation step)

Run with: python -m evaluation.cli <subcommand> [args...]
"""

from __future__ import annotations
import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT / "bf_worker"))
sys.path.insert(0, str(_ROOT))

from evaluation.fixture import discover

# NOTE: evaluation.runner pulls in the agent stack (langgraph + LLM client +
# settings) which is multi-second to import. We only need it inside `cmd_run`,
# so it's imported lazily there. Other subcommands (list-*, report, promote)
# stay snappy.

logger = logging.getLogger(__name__)

_FIXTURES_ROOT = _HERE / "fixtures"
_JOURNAL_ROOT = _HERE / "journal"


# ── Subcommand handlers ──────────────────────────────────────────────────────

def cmd_list_fixtures(args) -> int:
    fixtures = discover()
    if not fixtures:
        print(f"No fixtures yet. Add one under {_FIXTURES_ROOT}/<id>/source/.")
        return 0
    print(f"{len(fixtures)} fixtures:")
    for f in fixtures:
        print(f"  {f.fixture_id:30s}  {f.category:15s}  {f.difficulty:8s}  test_cmd={f.test_cmd!r}")
    return 0


def cmd_list_journal(args) -> int:
    if not _JOURNAL_ROOT.is_dir():
        print(f"No journal entries yet at {_JOURNAL_ROOT}.")
        return 0
    entries = sorted(p for p in _JOURNAL_ROOT.iterdir() if p.is_dir())
    shown = 0
    for e in entries:
        if args.flagged and not (e / "FLAGGED").exists():
            continue
        record_path = e / "record.json"
        if not record_path.exists():
            continue
        rec = json.loads(record_path.read_text(encoding="utf-8"))
        flagged = "★" if (e / "FLAGGED").exists() else " "
        print(f"  {flagged} {e.name:60s}  outcome={rec.get('outcome'):10s}  iter={rec.get('iterations')}")
        shown += 1
    print(f"\n({shown} entries{' flagged' if args.flagged else ''})")
    return 0


def cmd_run(args) -> int:
    # Lazy import: only when actually running a sweep do we pay the agent-stack import cost.
    from evaluation.coordinator import run_coordinated_sweep

    fixtures = discover()
    if not fixtures:
        print("No fixtures to run against. See 'bench list-fixtures'.", file=sys.stderr)
        return 2

    if args.config:
        agent_specs = json.loads(Path(args.config).read_text(encoding="utf-8"))
        if isinstance(agent_specs, dict):
            agent_specs = [agent_specs]
    else:
        agent_specs = [{"name": "langgraph", "agent": "langgraph", "kwargs": {}}]

    if args.fixture_id:
        fixtures = [f for f in fixtures if f.fixture_id in args.fixture_id]
        if not fixtures:
            print(f"No matching fixtures: {args.fixture_id}", file=sys.stderr)
            return 2

    run_dir = run_coordinated_sweep(agent_specs=agent_specs, fixtures=fixtures, run_id=args.run_id)
    print(f"\nDone. Run dir: {run_dir}")
    print(f"Report: python -m evaluation.cli report {run_dir.name}")
    return 0


def cmd_report(args) -> int:
    from evaluation.metrics import aggregate, format_table
    rows = aggregate(args.run_id)
    print(format_table(rows))
    return 0


def cmd_promote(args) -> int:
    """Promote a journal entry into a fixture.

    Copies the journal's recorded source snapshot (when present) plus trace into
    a new fixtures/<fixture_id>/ directory, and writes meta.json. The user is
    expected to fill in/refine meta.json afterwards.
    """
    journal_dir = _JOURNAL_ROOT / args.journal_entry
    if not journal_dir.is_dir():
        print(f"No such journal entry: {journal_dir}", file=sys.stderr)
        return 2

    fixture_id = args.fixture_id or args.journal_entry
    fixture_dir = _FIXTURES_ROOT / fixture_id
    if fixture_dir.exists():
        print(f"Fixture already exists: {fixture_dir}", file=sys.stderr)
        return 2

    rec = json.loads((journal_dir / "record.json").read_text(encoding="utf-8"))

    fixture_dir.mkdir(parents=True)
    # Source snapshot is not yet captured eagerly by the journal (running mode
    # operates on a live repo). Until that's added, the promote step asks the
    # user to populate source/ manually.
    (fixture_dir / "source").mkdir()

    if (journal_dir / "trace.txt").exists():
        shutil.copy2(journal_dir / "trace.txt", fixture_dir / "trace.txt")

    meta = {
        "fixture_id":       fixture_id,
        "promoted_from":    args.journal_entry,
        "category":         args.category or "unknown",
        "difficulty":       args.difficulty or "medium",
        "expected_outcome": rec.get("outcome", "fixed"),
        "test_cmd":         "pytest",
        "notes":            f"Promoted from journal entry {args.journal_entry}.",
    }
    (fixture_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print(f"Created fixture: {fixture_dir}")
    print(f"  Next: populate {fixture_dir / 'source'} with the buggy source tree")
    print(f"        and edit {fixture_dir / 'meta.json'} as needed.")
    return 0


# ── argparse wiring ──────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bench", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-fixtures").set_defaults(fn=cmd_list_fixtures)

    lj = sub.add_parser("list-journal")
    lj.add_argument("--flagged", action="store_true",
                    help="show only auto-flagged entries")
    lj.set_defaults(fn=cmd_list_journal)

    rn = sub.add_parser("run")
    rn.add_argument("--config", help="Path to JSON file: list of agent specs")
    rn.add_argument("--run-id", help="Optional run id (default: timestamp)")
    rn.add_argument("--fixture-id", nargs="*", help="Limit to specific fixture ids")
    rn.set_defaults(fn=cmd_run)

    rp = sub.add_parser("report")
    rp.add_argument("run_id")
    rp.set_defaults(fn=cmd_report)

    pr = sub.add_parser("promote")
    pr.add_argument("journal_entry", help="Directory name under evaluation/journal/")
    pr.add_argument("--fixture-id", help="Override fixture id (default: same as journal entry)")
    pr.add_argument("--category", help="e.g. off-by-one, race, type, recursion")
    pr.add_argument("--difficulty", help="easy | medium | hard")
    pr.set_defaults(fn=cmd_promote)

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [bench %(name)s] %(message)s",
        stream=sys.stdout,
    )
    args = _build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
