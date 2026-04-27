"""
Metrics — aggregate run records into a comparison report.

Reads evaluation/runs/<run_id>/summary.json and produces a per-agent table:
    agent_name | n_fixtures | n_fixed | fix_rate | avg_iterations | avg_elapsed_s
"""

from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path

_RUNS_ROOT = Path(__file__).resolve().parent / "runs"


def aggregate(run_id: str) -> list[dict]:
    """Group a sweep's records by agent and compute summary stats."""
    summary_path = _RUNS_ROOT / run_id / "summary.json"
    if not summary_path.exists():
        raise FileNotFoundError(f"no such run: {run_id}")
    records = json.loads(summary_path.read_text(encoding="utf-8"))

    by_agent: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_agent[r["agent_name"]].append(r)

    rows = []
    for agent_name, recs in sorted(by_agent.items()):
        n = len(recs)
        n_fixed = sum(1 for r in recs if r.get("outcome") == "fixed")
        n_match = sum(1 for r in recs if r.get("matches_expected"))
        rows.append({
            "agent_name":       agent_name,
            "n_fixtures":       n,
            "n_fixed":          n_fixed,
            "fix_rate":         round(n_fixed / n, 3) if n else 0.0,
            "n_match_expected": n_match,
            "match_rate":       round(n_match / n, 3) if n else 0.0,
            "avg_iterations":   round(sum(r.get("iterations", 0) for r in recs) / n, 2) if n else 0,
            "avg_elapsed_s":    round(sum(r.get("elapsed_s", 0) for r in recs) / n, 2) if n else 0,
        })
    return rows


def format_table(rows: list[dict]) -> str:
    if not rows:
        return "(no data)"
    headers = list(rows[0].keys())
    widths = {h: max(len(h), max(len(str(r[h])) for r in rows)) for h in headers}
    line = "  ".join(h.ljust(widths[h]) for h in headers)
    sep = "  ".join("-" * widths[h] for h in headers)
    body = "\n".join(
        "  ".join(str(r[h]).ljust(widths[h]) for h in headers) for r in rows
    )
    return f"{line}\n{sep}\n{body}"
