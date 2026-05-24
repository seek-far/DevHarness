"""
Metrics — aggregate run records into a comparison report.

Reads evaluation/runs/<run_id>/summary.json and produces a per-agent table:
    agent_name | n_fixtures | n_fixed | fix_rate | avg_iterations | avg_elapsed_s
    | avg_total_tokens | avg_llm_wallclock_s | tokens_per_s
"""

from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path

_RUNS_ROOT = Path(__file__).resolve().parent / "runs"


def _mean(values: list[float]) -> float | None:
    """Mean over non-None values, or None when no value was reported.

    Returning None instead of 0 keeps "reported zero" and "never reported"
    distinguishable in the table — important for cells whose backend doesn't
    surface usage at all.
    """
    xs = [v for v in values if v is not None]
    if not xs:
        return None
    return sum(xs) / len(xs)


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
        # Per-cell total tokens = prompt + completion; only counted when both
        # were reported (i.e. cell has llm_call_count > 0). tokens_per_s uses
        # the sum-of-sums to avoid double-averaging.
        per_cell_total_tokens: list[float] = []
        per_cell_wallclock: list[float] = []
        sum_tokens = 0.0
        sum_wallclock = 0.0
        for r in recs:
            ptt = r.get("total_prompt_tokens")
            ctt = r.get("total_completion_tokens")
            ws  = r.get("total_llm_wallclock_s")
            if ptt is not None and ctt is not None:
                per_cell_total_tokens.append(ptt + ctt)
                sum_tokens += ptt + ctt
            if ws is not None:
                per_cell_wallclock.append(ws)
                sum_wallclock += ws
        avg_total_tokens   = _mean(per_cell_total_tokens)
        avg_llm_wallclock  = _mean(per_cell_wallclock)
        tokens_per_s       = (sum_tokens / sum_wallclock) if sum_wallclock > 0 else None
        rows.append({
            "agent_name":          agent_name,
            "n_fixtures":          n,
            "n_fixed":             n_fixed,
            "fix_rate":            round(n_fixed / n, 3) if n else 0.0,
            "n_match_expected":    n_match,
            "match_rate":          round(n_match / n, 3) if n else 0.0,
            "avg_iterations":      round(sum(r.get("iterations", 0) for r in recs) / n, 2) if n else 0,
            "avg_elapsed_s":       round(sum(r.get("elapsed_s", 0) for r in recs) / n, 2) if n else 0,
            "avg_total_tokens":    round(avg_total_tokens, 1) if avg_total_tokens is not None else None,
            "avg_llm_wallclock_s": round(avg_llm_wallclock, 2) if avg_llm_wallclock is not None else None,
            "tokens_per_s":        round(tokens_per_s, 1) if tokens_per_s is not None else None,
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
