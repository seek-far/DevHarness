from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.coordinator import (  # noqa: E402
    _merge_summary,
    group_specs_by_agent_ref,
    normalize_agent_ref,
)


def test_normalize_agent_ref_defaults_to_current():
    assert normalize_agent_ref({}) == ""
    assert normalize_agent_ref({"agent_ref": None}) == ""
    assert normalize_agent_ref({"agent_ref": "current"}) == ""
    assert normalize_agent_ref({"agent_ref": " working-tree "}) == ""


def test_group_specs_by_agent_ref():
    specs = [
        {"name": "baseline"},
        {"name": "current-explicit", "agent_ref": "current"},
        {"name": "old", "agent_ref": "abc123"},
        {"name": "branch", "agent_ref": "feature/memory"},
    ]

    grouped = group_specs_by_agent_ref(specs)

    assert [s["name"] for s in grouped[""]] == ["baseline", "current-explicit"]
    assert [s["name"] for s in grouped["abc123"]] == ["old"]
    assert [s["name"] for s in grouped["feature/memory"]] == ["branch"]


def test_merge_summary_collects_cell_records(tmp_path: Path):
    run_dir = tmp_path / "run"
    first = run_dir / "agent-a" / "F01"
    second = run_dir / "agent-b" / "F02"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "record.json").write_text(json.dumps({"agent_name": "agent-a"}), encoding="utf-8")
    (second / "record.json").write_text(json.dumps({"agent_name": "agent-b"}), encoding="utf-8")

    _merge_summary(run_dir)

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary == [{"agent_name": "agent-a"}, {"agent_name": "agent-b"}]

