"""
Fixture — one bug in the benchmark.

Layout on disk:
    evaluation/fixtures/<fixture_id>/
        source/         # buggy source tree (working copy, not a git repo)
        trace.txt       # captured failure trace (optional; if absent, test_cmd is run)
        meta.json       # category, difficulty, test_cmd, expected_outcome, notes
        expected.patch  # OPTIONAL — known-good fix; used for patch-similarity metrics

Curation flow: a journal entry from running mode is promoted to a fixture by
copying its frozen source snapshot + trace into this layout and writing meta.json.
"""

from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path

_FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures"


@dataclass
class Fixture:
    fixture_id: str
    source_dir: Path
    trace_file: Path | None
    test_cmd: str = "pytest"
    category: str = "unknown"
    difficulty: str = "medium"
    expected_outcome: str = "fixed"
    notes: str = ""
    expected_patch: Path | None = None

    @classmethod
    def load(cls, fixture_dir: Path) -> "Fixture":
        meta_path = fixture_dir / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}

        source_dir = fixture_dir / "source"
        if not source_dir.is_dir():
            raise ValueError(f"fixture {fixture_dir} has no source/ dir")

        trace_file = fixture_dir / "trace.txt"
        expected_patch = fixture_dir / "expected.patch"

        return cls(
            fixture_id=fixture_dir.name,
            source_dir=source_dir,
            trace_file=trace_file if trace_file.exists() else None,
            test_cmd=meta.get("test_cmd", "pytest"),
            category=meta.get("category", "unknown"),
            difficulty=meta.get("difficulty", "medium"),
            expected_outcome=meta.get("expected_outcome", "fixed"),
            notes=meta.get("notes", ""),
            expected_patch=expected_patch if expected_patch.exists() else None,
        )


def discover(root: Path | None = None) -> list[Fixture]:
    """Return all fixtures under evaluation/fixtures/, sorted by id."""
    root = root or _FIXTURES_ROOT
    if not root.is_dir():
        return []
    fixtures = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if not (child / "source").is_dir():
            continue
        fixtures.append(Fixture.load(child))
    return fixtures
