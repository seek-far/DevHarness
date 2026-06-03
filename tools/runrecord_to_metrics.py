#!/usr/bin/env python3
"""
tools/runrecord_to_metrics.py — emit Prometheus metrics derived from journal.

Worker is a one-shot subprocess that writes a RunRecord on exit; it has
nowhere to keep a running Prometheus counter. This script bridges that
gap by scanning `evaluation/journal/<ts>_<bug>_<agent>_<model>/record.json`
and aggregating into Prometheus exposition-format text, written to a
file that node_exporter's textfile collector scrapes.

Counters emitted (cumulative across the entire journal — Prometheus
computes rates from the time-series):

  sdlcma_runs_total{agent, model_slug, outcome}
      Number of RunRecords (i.e. completed runs, success or not) by
      agent_name, slugified llm_model (slashes → dashes, cap 60 chars
      to bound cardinality), and outcome ∈ {fixed, error, no_fix,
      already_fixed}.

  sdlcma_run_elapsed_seconds_total{agent, model_slug, outcome}
      Sum of elapsed_s across the same buckets — divide by
      runs_total for mean wallclock.

  sdlcma_llm_tokens_total{type, model_slug}
      Sum of prompt / completion / cached input tokens, by token kind.
      Cost driver.

  sdlcma_llm_calls_total{agent, model_slug}
      Sum of llm_call_count across all RunRecords — per-agent total
      LLM volume.

  sdlcma_parse_trace_fallback_total{agent}
      Count of RunRecords whose parse_trace_fallback=True. Was 100%
      on GitLab-wrapped traces before 2026-05-29's parser fix; high
      values on this metric flag a parser regression.

  sdlcma_reflection_fires_total{agent}
      Count of RunRecords with reflection_count ≥ 1. Useful for
      reflection-enhancement A/B telemetry.

Gauge:
  sdlcma_runrecord_last_scan_timestamp
      Unix seconds of the last successful scan. Stale value =
      scanner cron broken.

Cardinality: agent ∈ small enum; model_slug bounded by per-run env
declaration (and capped at 60 chars); outcome ∈ 4 values; token type
∈ {prompt,completion,cached}. NEVER tagged by bug_id or project_id —
the same anti-pattern that lives on the gateway/orchestrator sides.

Cron usage:
  */1 * * * * cd /opt/sdlcma && uv run python tools/runrecord_to_metrics.py \\
      --journal-dir evaluation/journal \\
      --out /var/lib/node_exporter/textfile_collector/sdlcma_runrecord.prom

The script writes atomically (.tmp + rename) so a partial scrape from
node_exporter can't see a half-written file.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger("runrecord_to_metrics")

# Bound model_slug cardinality. The same slugification rule the journal
# writer uses for its directory naming — cap at 60 chars, slashes to
# dashes, alnum+`-_` only. Keeps a misconfigured llm_model that contains
# a URL or path from blowing up cardinality.
_MODEL_SLUG_MAX = 60


def slugify_model(name: str | None) -> str:
    if not name:
        return "unknown"
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", name)
    return s[:_MODEL_SLUG_MAX] or "unknown"


def _aggregate(records: list[dict]) -> dict:
    """Walk the parsed records once and emit a dict-of-counters keyed
    by (metric_name, frozenset(label_pairs)). Aggregation is total +
    sum, which Prometheus then divides on the dashboard side.

    Defensive parsing: every field has a default. A RunRecord that
    pre-dates a schema field shows up as None and is treated as zero
    (or skipped, for the parse_trace_fallback counter where only True
    counts)."""
    fixes = defaultdict(int)         # (agent, model, outcome) -> count
    elapsed = defaultdict(float)     # (agent, model, outcome) -> sum
    tokens = defaultdict(int)        # (type, model) -> sum
    llm_calls = defaultdict(int)     # (agent, model) -> sum
    parse_fb = defaultdict(int)      # agent -> count
    reflection = defaultdict(int)    # agent -> count

    for r in records:
        agent = r.get("agent_name") or "unknown"
        model_slug = slugify_model(r.get("llm_model"))
        outcome = r.get("outcome") or "unknown"

        fixes[(agent, model_slug, outcome)] += 1
        if isinstance(r.get("elapsed_s"), (int, float)):
            elapsed[(agent, model_slug, outcome)] += float(r["elapsed_s"])
        for token_field, type_name in (
            ("total_prompt_tokens", "prompt"),
            ("total_completion_tokens", "completion"),
            ("total_cached_input_tokens", "cached"),
        ):
            v = r.get(token_field)
            if isinstance(v, int) and v > 0:
                tokens[(type_name, model_slug)] += v
        v = r.get("llm_call_count")
        if isinstance(v, int) and v > 0:
            llm_calls[(agent, model_slug)] += v
        if r.get("parse_trace_fallback") is True:
            parse_fb[agent] += 1
        v = r.get("reflection_count")
        if isinstance(v, int) and v >= 1:
            reflection[agent] += 1

    return {
        "fixes": fixes,
        "elapsed": elapsed,
        "tokens": tokens,
        "llm_calls": llm_calls,
        "parse_fb": parse_fb,
        "reflection": reflection,
    }


def _scan_journal(journal_dir: Path) -> list[dict]:
    """Read every record.json under journal_dir. Malformed files log
    a warning but don't crash — a half-written record from an
    in-progress fix shouldn't break the cron."""
    out: list[dict] = []
    if not journal_dir.is_dir():
        logger.warning("journal dir does not exist: %s", journal_dir)
        return out
    for entry in journal_dir.iterdir():
        if not entry.is_dir():
            continue
        record_path = entry / "record.json"
        if not record_path.is_file():
            continue
        try:
            data = json.loads(record_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out.append(data)
        except (OSError, ValueError) as exc:
            logger.warning("skipping %s: %s", record_path, exc)
    return out


def _escape(value: str) -> str:
    """Prometheus label value escaping per exposition format:
    backslash, double-quote, and newline."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_counter(name: str, help_text: str,
                    samples: dict[tuple, float],
                    label_names: tuple[str, ...]) -> list[str]:
    """Render a single COUNTER family as HELP + TYPE + sample lines."""
    lines = [
        f"# HELP {name} {help_text}",
        f"# TYPE {name} counter",
    ]
    for labels, value in sorted(samples.items()):
        label_str = ",".join(
            f'{k}="{_escape(v)}"' for k, v in zip(label_names, labels)
        )
        lines.append(f"{name}{{{label_str}}} {value}")
    return lines


def _render_gauge(name: str, help_text: str, value: float) -> list[str]:
    return [
        f"# HELP {name} {help_text}",
        f"# TYPE {name} gauge",
        f"{name} {value}",
    ]


def render(agg: dict, scan_ts: float) -> str:
    """Compose the full exposition-format payload."""
    lines: list[str] = []
    lines += _render_counter(
        "sdlcma_runs_total",
        "Total RunRecords (completed runs, success or not) by agent, llm_model, and outcome",
        dict(agg["fixes"]),
        ("agent", "model_slug", "outcome"),
    )
    lines += _render_counter(
        "sdlcma_run_elapsed_seconds_total",
        "Sum of elapsed_s per RunRecord bucket (divide by runs_total for mean)",
        dict(agg["elapsed"]),
        ("agent", "model_slug", "outcome"),
    )
    lines += _render_counter(
        "sdlcma_llm_tokens_total",
        "Sum of token counts across RunRecords by token type and model",
        dict(agg["tokens"]),
        ("type", "model_slug"),
    )
    lines += _render_counter(
        "sdlcma_llm_calls_total",
        "Sum of llm_call_count across RunRecords",
        dict(agg["llm_calls"]),
        ("agent", "model_slug"),
    )
    lines += _render_counter(
        "sdlcma_parse_trace_fallback_total",
        "RunRecords where parse_trace_fallback=True (parser regression signal)",
        {(k,): v for k, v in agg["parse_fb"].items()},
        ("agent",),
    )
    lines += _render_counter(
        "sdlcma_reflection_fires_total",
        "RunRecords with reflection_count >= 1",
        {(k,): v for k, v in agg["reflection"].items()},
        ("agent",),
    )
    lines += _render_gauge(
        "sdlcma_runrecord_last_scan_timestamp",
        "Unix seconds of the last successful runrecord_to_metrics scan",
        scan_ts,
    )
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    """Write the .prom file atomically so node_exporter never reads a
    half-written file. tempfile + rename(2) is atomic on POSIX."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--journal-dir",
                   default=str(Path(__file__).resolve().parents[1]
                               / "evaluation" / "journal"),
                   help="Path to evaluation/journal/ (default: repo root)")
    p.add_argument("--out", required=True,
                   help="Output .prom file path (e.g. "
                        "/var/lib/node_exporter/textfile_collector/sdlcma_runrecord.prom)")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    journal = Path(args.journal_dir)
    records = _scan_journal(journal)
    logger.info("scanned %d RunRecord(s) under %s", len(records), journal)

    agg = _aggregate(records)
    body = render(agg, time.time())

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(out_path, body)
    logger.info("wrote %d bytes to %s", len(body), out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
