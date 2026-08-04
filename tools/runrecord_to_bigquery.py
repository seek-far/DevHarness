#!/usr/bin/env python3
"""
tools/runrecord_to_bigquery.py — export the RunRecord journal to BigQuery.

Third consumer of `evaluation/journal/`, alongside `runrecord_to_metrics.py`
(node_exporter textfile) and `runrecord_exporter.py` (HTTP /metrics). Those two
answer "how is the system doing right now"; this one answers "what happened
across every run, on every host, over time" — the question a Prometheus counter
structurally cannot, because it aggregates away the per-run detail.

WHY IT EXISTS (the honest version): 425 journal entries × 71 RunRecord fields,
of which `bench list-journal` surfaces exactly two. Every cross-run question
today needs a throwaway Python script. At 18 MB, DuckDB would serve equally
well — the reason it is BigQuery is that the journal is SPLIT ACROSS HOSTS
(minus, ls4900, and both k3s nodes), and a central table is the cheap way to
make cross-host runs visible at all. `docs/architecture.md` W4 already records
the symptom: "the journal splits per node so cross-node runs are invisible to
Grafana (read the directory, not the dashboard)".

DESIGN NOTES (each is load-bearing, not preference):

  * **Idempotent by `entry_id`.** BigQuery has no primary key, no UNIQUE
    constraint and no UPSERT — a repeated load simply duplicates rows. The
    journal directory name (`<ts>_<bug_id>_<agent>_<model>`) is already unique,
    so it becomes `entry_id`; we read the existing ids and append only the
    difference. WRITE_TRUNCATE would be simpler but is WRONG here: two hosts
    exporting to one table would take turns wiping each other, defeating the
    whole point.

  * **NULL stays NULL.** A missing `total_cost_usd` must not become 0.0 — the
    project invariant is that an unknown cost reads as blank, never as "this
    was free". Same for token counts on backends that report no usage. Filling
    zeros would silently poison every AVG() downstream.

  * **Additive schema evolution.** RunRecord evolves by adding fields
    (invariant #5). The load job sets ALLOW_FIELD_ADDITION so a new field
    lands as a new column with NULL for older rows, and this exporter needs no
    change when that happens.

  * **Load jobs, not streaming inserts.** `insert_rows_json` costs $0.05/GB and
    buys real-time visibility we do not need; `load_table_from_json` is free.

Usage:
    python -m tools.runrecord_to_bigquery --dry-run
    python -m tools.runrecord_to_bigquery --dataset sdlcma --table runs
    BF_JOURNAL_DIR=/var/sdlcma/journal python -m tools.runrecord_to_bigquery
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import fields as dataclass_fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bf_worker"))

from agents.run_record import RunRecord  # noqa: E402

logger = logging.getLogger("runrecord_to_bigquery")

DEFAULT_DATASET = "sdlcma"
DEFAULT_TABLE = "runs"
# Dataset location is FIXED AT CREATION and cannot be altered afterwards; a
# query may not join tables across locations either. "US" is the multi-region
# the free tier is denominated in. Unrelated to the Vertex region.
DEFAULT_LOCATION = "US"

# The RunRecord timestamp is a compact UTC string, e.g. "20260804T152240Z".
_TS_FORMAT = "%Y%m%dT%H%M%SZ"

# Synthetic columns this exporter adds on top of the RunRecord fields.
ENTRY_ID_COLUMN = "entry_id"
RUN_TS_COLUMN = "run_ts"


# ── journal reading ──────────────────────────────────────────────────────────


def iter_journal_entries(journal_dir: Path) -> Iterator[tuple[str, dict]]:
    """Yield (entry_id, record) for every readable record.json.

    Deliberately NOT reusing `runrecord_to_metrics._scan_journal`: that one
    returns records only, while the directory NAME is exactly what makes this
    exporter idempotent. Malformed files warn and are skipped — a half-written
    record from an in-flight fix must not abort a cron-driven export.
    """
    if not journal_dir.is_dir():
        logger.warning("journal dir does not exist: %s", journal_dir)
        return
    for entry in sorted(journal_dir.iterdir()):
        if not entry.is_dir():
            continue
        record_path = entry / "record.json"
        if not record_path.is_file():
            continue
        try:
            data = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("skipping %s: %s", record_path, exc)
            continue
        if isinstance(data, dict):
            yield entry.name, data


# ── schema derivation ────────────────────────────────────────────────────────

# Scalar list fields worth querying element-wise (UNNEST for percentiles etc.).
# Everything else list-shaped is a list of objects and goes to JSON text.
_SCALAR_ARRAY_FIELDS = {"llm_call_wallclock_ms": "INT64"}


def bq_type_for(annotation: str) -> str:
    """Map a RunRecord annotation string to a BigQuery scalar type.

    RunRecord uses `from __future__ import annotations`, so field types arrive
    as strings ("int | None"). Order matters: a union like `int | str | None`
    contains both "int" and "str" and must be caught BEFORE either — BigQuery
    columns are single-typed, and STRING is the only lossless target for a
    field that is sometimes numeric (GitLab ids are the real case).
    """
    a = annotation.replace(" ", "")
    if "dict" in a or "list" in a:
        return "JSON_TEXT"          # resolved by build_schema
    if "int" in a and "str" in a:
        return "STRING"
    if "bool" in a:
        return "BOOL"
    if "float" in a:
        return "FLOAT64"
    if "int" in a:
        return "INT64"
    return "STRING"


def build_schema():
    """BigQuery schema derived from the RunRecord dataclass + two synthetic
    columns. Every RunRecord column is NULLABLE: most fields are Optional, and
    "unknown" must be representable as NULL rather than coerced to 0/''.
    """
    from google.cloud import bigquery

    schema = [
        bigquery.SchemaField(
            ENTRY_ID_COLUMN, "STRING", mode="REQUIRED",
            description="journal directory name; the idempotency key",
        ),
        bigquery.SchemaField(
            RUN_TS_COLUMN, "TIMESTAMP",
            description="parsed from RunRecord.timestamp; partition column",
        ),
    ]
    for f in dataclass_fields(RunRecord):
        if f.name in _SCALAR_ARRAY_FIELDS:
            schema.append(
                bigquery.SchemaField(
                    f.name, _SCALAR_ARRAY_FIELDS[f.name], mode="REPEATED"
                )
            )
            continue
        kind = bq_type_for(str(f.type))
        if kind == "JSON_TEXT":
            # STRING holding JSON text, not the native JSON type. The native
            # type is better ergonomically, but its NDJSON load path is fussy
            # about whether a value arrives as a nested object or as a string,
            # and getting it wrong fails at load time against a real dataset —
            # exactly the thing a mocked test cannot catch. STRING is
            # functionally equivalent for querying (JSON_VALUE / PARSE_JSON
            # both accept it) and can be promoted with one ALTER later.
            schema.append(bigquery.SchemaField(f.name, "STRING"))
        else:
            schema.append(bigquery.SchemaField(f.name, kind))
    return schema


# ── row coercion ─────────────────────────────────────────────────────────────


def parse_run_ts(record: dict, entry_id: str) -> str | None:
    """RFC-3339 timestamp for the partition column, or None.

    Tries RunRecord.timestamp first, then the entry_id prefix (the directory
    name starts with the same stamp). None when neither parses — the row still
    loads, it just lands in the NULL partition rather than being dropped.
    """
    for candidate in (record.get("timestamp"), entry_id.split("_", 1)[0]):
        if not isinstance(candidate, str) or not candidate:
            continue
        try:
            dt = datetime.strptime(candidate, _TS_FORMAT).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        return dt.isoformat()
    return None


def coerce_row(entry_id: str, record: dict) -> dict[str, Any]:
    """One journal record → one BigQuery row, matching build_schema()."""
    row: dict[str, Any] = {
        ENTRY_ID_COLUMN: entry_id,
        RUN_TS_COLUMN: parse_run_ts(record, entry_id),
    }
    for f in dataclass_fields(RunRecord):
        value = record.get(f.name)
        # None in, None out. Never a zero, never an empty string: a fabricated
        # value is indistinguishable from a real one once it is in the table.
        if value is None:
            row[f.name] = None
            continue

        if f.name in _SCALAR_ARRAY_FIELDS:
            row[f.name] = (
                [v for v in value if isinstance(v, (int, float))]
                if isinstance(value, list) else []
            )
            continue

        kind = bq_type_for(str(f.type))
        if kind == "JSON_TEXT":
            row[f.name] = json.dumps(value, ensure_ascii=False, default=str)
        elif kind == "BOOL":
            row[f.name] = bool(value)
        elif kind == "INT64":
            try:
                row[f.name] = int(value)
            except (TypeError, ValueError):
                row[f.name] = None
        elif kind == "FLOAT64":
            try:
                row[f.name] = float(value)
            except (TypeError, ValueError):
                row[f.name] = None
        else:
            row[f.name] = value if isinstance(value, str) else str(value)
    return row


# ── BigQuery side ────────────────────────────────────────────────────────────


def ensure_table(client, table_ref, location: str):
    """Create dataset + table if absent. Idempotent (exists_ok)."""
    from google.cloud import bigquery

    dataset_ref = bigquery.DatasetReference(table_ref.project, table_ref.dataset_id)
    dataset = bigquery.Dataset(dataset_ref)
    dataset.location = location
    client.create_dataset(dataset, exists_ok=True)

    table = bigquery.Table(table_ref, schema=build_schema())
    # DAY partitioning on run_ts: BigQuery bills by bytes scanned, so a
    # "last 30 days" query stops reading the rest of the table. Free at today's
    # size; the point is that ver99-scale batches make the table grow fast.
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY, field=RUN_TS_COLUMN
    )
    return client.create_table(table, exists_ok=True)


def existing_entry_ids(client, table_ref) -> set[str]:
    """The idempotency read. Scans one STRING column, so it is measured in KB
    against a 1 TB/month free query allowance."""
    query = (
        f"SELECT {ENTRY_ID_COLUMN} FROM "
        f"`{table_ref.project}.{table_ref.dataset_id}.{table_ref.table_id}`"
    )
    return {row[ENTRY_ID_COLUMN] for row in client.query(query).result()}


def load_rows(client, table_ref, rows: list[dict]) -> int:
    from google.cloud import bigquery

    job_config = bigquery.LoadJobConfig(
        schema=build_schema(),
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        # Invariant #5 is additive-only, so a RunRecord that grew a field must
        # not fail the load; it becomes a new column, NULL for existing rows.
        schema_update_options=[
            bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION,
        ],
    )
    job = client.load_table_from_json(rows, table_ref, job_config=job_config)
    job.result()  # raises on failure
    return len(rows)


# ── orchestration ────────────────────────────────────────────────────────────


def export(
    journal_dir: Path,
    dataset: str = DEFAULT_DATASET,
    table: str = DEFAULT_TABLE,
    project: str | None = None,
    location: str = DEFAULT_LOCATION,
    dry_run: bool = False,
) -> dict[str, int]:
    entries = list(iter_journal_entries(journal_dir))
    stats = {"scanned": len(entries), "skipped_existing": 0, "loaded": 0}

    if dry_run:
        logger.info(
            "dry-run: %d journal entries, %d columns, target %s.%s (location=%s)",
            len(entries), len(build_schema_names()), dataset, table, location,
        )
        return stats

    from google.cloud import bigquery

    client = bigquery.Client(project=project)
    table_ref = bigquery.TableReference(
        bigquery.DatasetReference(client.project, dataset), table
    )
    ensure_table(client, table_ref, location)

    known = existing_entry_ids(client, table_ref)
    rows = [
        coerce_row(entry_id, record)
        for entry_id, record in entries
        if entry_id not in known
    ]
    stats["skipped_existing"] = len(entries) - len(rows)

    if not rows:
        logger.info("nothing new to load (%d entries already present)", len(known))
        return stats

    stats["loaded"] = load_rows(client, table_ref, rows)
    logger.info(
        "loaded %d new row(s) into %s.%s.%s (%d already present)",
        stats["loaded"], client.project, dataset, table, stats["skipped_existing"],
    )
    return stats


def build_schema_names() -> list[str]:
    """Column names without importing bigquery — used by --dry-run and tests."""
    names = [ENTRY_ID_COLUMN, RUN_TS_COLUMN]
    names += [f.name for f in dataclass_fields(RunRecord)]
    return names


def _default_journal_dir() -> Path:
    env = os.environ.get("BF_JOURNAL_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "evaluation" / "journal"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--journal-dir", default=None,
                        help="default: $BF_JOURNAL_DIR or evaluation/journal")
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--table", default=DEFAULT_TABLE)
    parser.add_argument("--project", default=None,
                        help="default: the ADC project")
    parser.add_argument("--location", default=DEFAULT_LOCATION,
                        help="dataset location; FIXED at creation (default: US)")
    parser.add_argument("--dry-run", action="store_true",
                        help="scan and report without touching BigQuery")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    journal_dir = Path(args.journal_dir) if args.journal_dir else _default_journal_dir()
    try:
        stats = export(
            journal_dir=journal_dir,
            dataset=args.dataset,
            table=args.table,
            project=args.project,
            location=args.location,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        logger.error("export failed: %s", exc)
        return 1

    print(
        f"scanned={stats['scanned']} "
        f"loaded={stats['loaded']} "
        f"skipped_existing={stats['skipped_existing']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
