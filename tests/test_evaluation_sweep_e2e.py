"""End-to-end test for the evaluation comparison arc.

Exercises the typical `bench run` + `bench report` flow without burning real
LLM tokens: the real fixture loader, real `run_sweep` orchestration, real
`RunRecord` serialization, and real `metrics.aggregate` aggregation are all
driven against a sandboxed `runs/` directory in `tmp_path`. The only stub is
the `Agent` itself, which is unavoidable for a deterministic, fast,
network-free test.

Closes the two 🔴 gaps in `tests/README.md` "Coverage gap: evaluation/":

  - `runner.run_sweep` happy-path orchestration is untested
  - `metrics.aggregate` is untested

And establishes a "no-field-left-behind" guard against future `RunRecord`
fields that aren't threaded through the runner: every key in
`RunRecord.__dataclass_fields__` must appear in each cell's record.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Match the import order used by evaluation.runner so its module-level
# `from agents.base import ...` resolves the same way.
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "bf_worker"))

from agents.base import Agent, BugInput, FixOutput  # noqa: E402
from agents.run_record import RunRecord  # noqa: E402
from evaluation import metrics, runner  # noqa: E402
from evaluation.fixture import discover  # noqa: E402


# ── stubs ────────────────────────────────────────────────────────────────────


class _StubAgent(Agent):
    """Agent that returns a pre-canned FixOutput keyed by fixture id.

    The runner only needs `agent.fix(bug_input) -> FixOutput`. We populate a
    minimal `final_state` so a few of the optional RunRecord telemetry fields
    (react_step_count, react_confidence) get exercised through
    `RunRecord.from_outputs`.
    """

    def __init__(self, name: str, outcome_per_fixture: dict[str, str]):
        self.name = name
        self._outcomes = outcome_per_fixture

    def fix(self, bug_input: BugInput) -> FixOutput:
        outcome = self._outcomes.get(bug_input.bug_id, "fixed")
        return FixOutput(
            outcome=outcome,
            bug_id=bug_input.bug_id,
            iterations=0 if outcome == "fixed" else 1,
            final_state={
                "react_step_count": 1,
                "react_confidence": "high" if outcome == "fixed" else None,
            },
        )


def _write_fixture(
    root: Path,
    fixture_id: str,
    source_files: dict[str, str],
    *,
    category: str = "off-by-one",
) -> None:
    """Build one fixture directory matching the layout that `Fixture.load` expects."""
    fdir = root / fixture_id
    (fdir / "source").mkdir(parents=True)
    for name, content in source_files.items():
        (fdir / "source" / name).write_text(content, encoding="utf-8")
    (fdir / "meta.json").write_text(
        json.dumps({
            "category": category,
            "difficulty": "easy",
            "expected_outcome": "fixed",
            "test_cmd": "pytest",
        }),
        encoding="utf-8",
    )
    (fdir / "trace.txt").write_text(
        "E AssertionError: synthetic\nfixture_dummy.py:1\n", encoding="utf-8"
    )


# ── e2e ──────────────────────────────────────────────────────────────────────


def test_evaluation_sweep_e2e(tmp_path: Path, monkeypatch):
    # 1. Two fixtures so the per-agent fix_rate is non-trivial (1/2 vs 2/2).
    fixtures_dir = tmp_path / "fixtures"
    _write_fixture(
        fixtures_dir, "F01_offbyone",
        {"calc.py": "def add(a,b): return a-b\n"},
        category="off-by-one",
    )
    _write_fixture(
        fixtures_dir, "F02_typeerr",
        {"conv.py": "def to_int(s): return s\n"},
        category="type-error",
    )

    # 2. Sandbox both `_RUNS_ROOT`s so the test never writes to evaluation/runs/.
    runs_dir = tmp_path / "runs"
    monkeypatch.setattr(runner, "_RUNS_ROOT", runs_dir)
    monkeypatch.setattr(metrics, "_RUNS_ROOT", runs_dir)

    # 3. Two stub agents: one always-fixes, one fixes only F01 → ground truth
    #    fix_rate is 1.0 and 0.5 respectively, easy to assert exactly.
    agents_by_name = {
        "always_fixes": _StubAgent("always_fixes", {
            "F01_offbyone": "fixed",
            "F02_typeerr": "fixed",
        }),
        "flaky": _StubAgent("flaky", {
            "F01_offbyone": "fixed",
            "F02_typeerr": "no_fix",
        }),
    }
    monkeypatch.setattr(
        runner, "make_agent", lambda spec: agents_by_name[spec["name"]]
    )

    # 4. Real Fixture loader — exercises evaluation/fixture.py.
    fixtures = discover(fixtures_dir)
    assert {f.fixture_id for f in fixtures} == {"F01_offbyone", "F02_typeerr"}
    # The category from meta.json must round-trip through the loader.
    by_id = {f.fixture_id: f for f in fixtures}
    assert by_id["F01_offbyone"].category == "off-by-one"
    assert by_id["F02_typeerr"].category == "type-error"

    # 5. Real run_sweep — exercises evaluation/runner.py end to end.
    specs = [
        {"name": "always_fixes", "agent": "stub"},
        {"name": "flaky",        "agent": "stub"},
    ]
    run_dir = runner.run_sweep(specs, fixtures, run_id="test_sweep_001")

    # 6. On-disk layout: one cell per (agent, fixture), plus a summary file.
    assert run_dir == runs_dir / "test_sweep_001"
    assert (run_dir / "summary.json").exists()
    for agent_name in ("always_fixes", "flaky"):
        for fid in ("F01_offbyone", "F02_typeerr"):
            cell = run_dir / agent_name / fid
            assert (cell / "record.json").exists(), (
                f"missing record for {agent_name}/{fid}"
            )

    # 7. RunRecord field completeness — every field declared on the dataclass
    #    must appear in the on-disk JSON. If a future field is added to
    #    `RunRecord` but the runner forgets to thread it through, this fails.
    sample = json.loads(
        (run_dir / "always_fixes" / "F01_offbyone" / "record.json").read_text(
            encoding="utf-8"
        )
    )
    record_fields = set(RunRecord.__dataclass_fields__.keys())
    missing = record_fields - sample.keys()
    assert not missing, f"runner failed to emit RunRecord fields: {missing}"

    # 8. Outcome correctness — the runner must surface what the agent returned.
    cells = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    by = {(r["agent_name"], r["bug_id"]): r for r in cells}
    assert by[("always_fixes", "F01_offbyone")]["outcome"] == "fixed"
    assert by[("always_fixes", "F02_typeerr")]["outcome"] == "fixed"
    assert by[("flaky", "F01_offbyone")]["outcome"] == "fixed"
    assert by[("flaky", "F02_typeerr")]["outcome"] == "no_fix"

    # 9. matches_expected is the agreement between actual outcome and the
    #    fixture's expected_outcome. Both fixtures expect 'fixed'.
    assert by[("always_fixes", "F01_offbyone")]["matches_expected"] is True
    assert by[("always_fixes", "F02_typeerr")]["matches_expected"] is True
    assert by[("flaky",         "F01_offbyone")]["matches_expected"] is True
    assert by[("flaky",         "F02_typeerr")]["matches_expected"] is False

    # 10. Real metrics.aggregate on the runner's actual output. This is the
    #     "I edited a config and ran the report" half of the typical arc.
    rows = metrics.aggregate("test_sweep_001")
    by_agent = {r["agent_name"]: r for r in rows}

    af = by_agent["always_fixes"]
    assert af["n_fixtures"] == 2
    assert af["n_fixed"] == 2
    assert af["fix_rate"] == 1.0
    assert af["match_rate"] == 1.0

    fl = by_agent["flaky"]
    assert fl["n_fixtures"] == 2
    assert fl["n_fixed"] == 1
    assert fl["fix_rate"] == 0.5
    assert fl["match_rate"] == 0.5
    # iterations: always_fixes returns 0 every time; flaky returns 0 for fixed
    # and 1 for no_fix.
    assert af["avg_iterations"] == 0
    assert fl["avg_iterations"] == 0.5


# ── small companion checks for metrics edge cases ─────────────────────────────


def test_metrics_format_table_handles_empty():
    """format_table on an empty rows list produces a sane placeholder, not a crash."""
    assert metrics.format_table([]) == "(no data)"


def test_metrics_aggregate_missing_run_raises(tmp_path: Path, monkeypatch):
    """aggregate() must raise FileNotFoundError on an unknown run_id rather
    than silently returning an empty list — operators rely on the failure to
    catch typos."""
    monkeypatch.setattr(metrics, "_RUNS_ROOT", tmp_path / "runs")
    with pytest.raises(FileNotFoundError):
        metrics.aggregate("does_not_exist")
