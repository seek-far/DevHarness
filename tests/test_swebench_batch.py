"""swebench_batch selection + resolved-rate metrics — no docker/LLM/dataset.

Pins:
  - filter_instances: ids (order preserved), regex filter, slice, shuffle,
    and the unknown-id guard;
  - metrics.aggregate additively surfaces resolved-rate for SWE-bench runs and
    leaves the F01–F10 fix-rate report untouched when no `resolved` is present.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "bf_worker"))

from swebench_batch import filter_instances  # noqa: E402
from evaluation import metrics  # noqa: E402


def _insts(*ids):
    return [{"instance_id": i} for i in ids]


_POOL = _insts("astropy__astropy-1", "sympy__sympy-3", "sympy__sympy-2", "django__django-9")


def test_filter_default_is_sorted_all():
    out = [i["instance_id"] for i in filter_instances(_POOL)]
    assert out == ["astropy__astropy-1", "django__django-9", "sympy__sympy-2", "sympy__sympy-3"]


def test_filter_ids_preserve_given_order():
    out = [i["instance_id"] for i in filter_instances(_POOL, ids=["sympy__sympy-3", "astropy__astropy-1"])]
    assert out == ["sympy__sympy-3", "astropy__astropy-1"]


def test_filter_unknown_id_raises():
    with pytest.raises(SystemExit):
        filter_instances(_POOL, ids=["nope__nope-1"])


def test_filter_regex():
    out = [i["instance_id"] for i in filter_instances(_POOL, filter_re=r"^sympy__")]
    assert out == ["sympy__sympy-2", "sympy__sympy-3"]


def test_filter_slice():
    out = [i["instance_id"] for i in filter_instances(_POOL, slice_spec="0:2")]
    assert out == ["astropy__astropy-1", "django__django-9"]


def test_filter_shuffle_is_deterministic():
    a = [i["instance_id"] for i in filter_instances(_POOL, shuffle=True)]
    b = [i["instance_id"] for i in filter_instances(_POOL, shuffle=True)]
    assert a == b and sorted(a) == sorted(i["instance_id"] for i in _POOL)


# ── metrics resolved-rate ─────────────────────────────────────────────────────

def _write_run(tmp_path, run_id, records):
    d = tmp_path / run_id
    d.mkdir(parents=True)
    (d / "summary.json").write_text(json.dumps(records), encoding="utf-8")


def test_metrics_surfaces_resolved_rate(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "_RUNS_ROOT", tmp_path)
    records = [
        {"agent_name": "mini_swe_agent", "outcome": "fixed", "resolved": True, "iterations": 3, "elapsed_s": 10},
        {"agent_name": "mini_swe_agent", "outcome": "fixed", "resolved": False, "iterations": 5, "elapsed_s": 20},
        {"agent_name": "mini_swe_agent", "outcome": "no_fix", "resolved": None, "iterations": 0, "elapsed_s": 5},
    ]
    _write_run(tmp_path, "r1", records)
    rows = metrics.aggregate("r1")

    assert len(rows) == 1
    row = rows[0]
    assert row["n_fixtures"] == 3
    assert row["n_fixed"] == 2          # patches submitted
    assert row["n_resolved"] == 1       # harness-passed
    assert row["resolved_rate"] == round(1 / 3, 3)


def test_metrics_omits_resolved_columns_without_resolved(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "_RUNS_ROOT", tmp_path)
    records = [
        {"agent_name": "baseline", "outcome": "fixed", "iterations": 1, "elapsed_s": 3,
         "matches_expected": True},
    ]
    _write_run(tmp_path, "r2", records)
    row = metrics.aggregate("r2")[0]

    assert "resolved_rate" not in row and "n_resolved" not in row
    assert row["fix_rate"] == 1.0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
