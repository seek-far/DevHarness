"""Tests for the journal → BigQuery exporter.

The load-bearing behaviours, each with a reason it is not just coverage:

  * **Idempotency by entry_id.** BigQuery has no primary key and no UPSERT, so
    "run the exporter twice" duplicates every row unless we filter. And the
    filter must be append-based: WRITE_TRUNCATE would have two hosts wiping
    each other's rows, destroying the cross-host aggregation the exporter
    exists for.
  * **NULL stays NULL.** Coercing an unknown cost/token count to 0 poisons
    every AVG() and reads as "this was free" — the same lie the project's
    cost invariant forbids upstream.
  * **Union types collapse to STRING.** `int | str | None` (GitLab ids) has no
    single numeric BigQuery type; catching it before the int/str branches is
    an ordering dependency that is easy to break silently.
  * **Schema covers every RunRecord field.** RunRecord grows additively
    (invariant #5); a field added upstream must not silently stop being
    exported.
"""

from __future__ import annotations

import json
import sys
from dataclasses import fields as dataclass_fields
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "bf_worker"))

from agents.run_record import RunRecord  # noqa: E402
from tools import runrecord_to_bigquery as rb  # noqa: E402


# ── type mapping ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "annotation,expected",
    [
        ("str", "STRING"),
        ("str | None", "STRING"),
        ("int", "INT64"),
        ("int | None", "INT64"),
        ("float | None", "FLOAT64"),
        ("bool | None", "BOOL"),
        ("dict", "JSON_TEXT"),
        ("dict | None", "JSON_TEXT"),
        ("list | None", "JSON_TEXT"),
    ],
)
def test_type_mapping(annotation, expected):
    assert rb.bq_type_for(annotation) == expected


def test_union_of_int_and_str_becomes_string():
    """`review_id: int | str | None` — GitLab ids arrive both ways. A BigQuery
    column is single-typed, so STRING is the only lossless target. This case
    MUST be tested before the plain int/str branches, which both also match."""
    assert rb.bq_type_for("int | str | None") == "STRING"


def test_real_runrecord_union_fields_are_strings():
    """Anchored on the actual dataclass, not a hand-written annotation."""
    by_name = {f.name: str(f.type) for f in dataclass_fields(RunRecord)}
    for name in ("review_id", "review_iid"):
        assert rb.bq_type_for(by_name[name]) == "STRING"


# ── schema ───────────────────────────────────────────────────────────────────


def test_schema_covers_every_runrecord_field():
    """A field added to RunRecord must not quietly stop being exported."""
    schema_names = {f.name for f in rb.build_schema()}
    for f in dataclass_fields(RunRecord):
        assert f.name in schema_names, f"RunRecord.{f.name} missing from schema"


def test_schema_adds_the_two_synthetic_columns():
    names = [f.name for f in rb.build_schema()]
    assert names[0] == rb.ENTRY_ID_COLUMN
    assert rb.RUN_TS_COLUMN in names


def test_entry_id_is_required_and_run_ts_is_a_timestamp():
    by_name = {f.name: f for f in rb.build_schema()}
    assert by_name[rb.ENTRY_ID_COLUMN].mode == "REQUIRED"
    assert by_name[rb.RUN_TS_COLUMN].field_type == "TIMESTAMP"


def test_runrecord_columns_are_all_nullable():
    """Optional-heavy schema: "unknown" must be representable."""
    synthetic = {rb.ENTRY_ID_COLUMN, rb.RUN_TS_COLUMN}
    for f in rb.build_schema():
        if f.name not in synthetic:
            assert f.mode in ("NULLABLE", "REPEATED"), f.name


def test_scalar_list_field_is_a_repeated_int():
    """llm_call_wallclock_ms as ARRAY<INT64> so UNNEST() can compute latency
    percentiles in SQL. Lists of objects go to JSON text instead."""
    by_name = {f.name: f for f in rb.build_schema()}
    fld = by_name["llm_call_wallclock_ms"]
    assert fld.mode == "REPEATED"
    assert fld.field_type == "INT64"


def test_object_list_field_is_not_repeated():
    by_name = {f.name: f for f in rb.build_schema()}
    assert by_name["code_review_findings"].mode == "NULLABLE"


# ── row coercion ─────────────────────────────────────────────────────────────


def test_none_stays_none_never_zero():
    """The invariant. An unknown cost rendered as 0.0 reads as "this was
    free"; an unknown token count as 0 drags every average down."""
    row = rb.coerce_row("e1", {"bug_id": "B1"})
    for key in ("total_cost_usd", "llm_call_count", "total_prompt_tokens",
                "code_review_status", "llm_backend_name"):
        assert row[key] is None, key


def test_dict_fields_are_serialised_to_json_text():
    payload = {"status": "success", "branch_name": "auto/bf/x"}
    row = rb.coerce_row("e1", {"branch_create_result": payload})
    assert json.loads(row["branch_create_result"]) == payload


def test_scalar_array_is_kept_as_a_list():
    row = rb.coerce_row("e1", {"llm_call_wallclock_ms": [6000, 10650, 26, 5]})
    assert row["llm_call_wallclock_ms"] == [6000, 10650, 26, 5]


def test_scalar_array_drops_non_numeric_junk():
    row = rb.coerce_row("e1", {"llm_call_wallclock_ms": [1, "x", None, 2]})
    assert row["llm_call_wallclock_ms"] == [1, 2]


def test_union_id_is_stringified_whichever_way_it_arrives():
    assert rb.coerce_row("e1", {"review_id": 42})["review_id"] == "42"
    assert rb.coerce_row("e1", {"review_id": "42"})["review_id"] == "42"


def test_unparseable_number_becomes_null_not_zero():
    row = rb.coerce_row("e1", {"llm_call_count": "not-a-number"})
    assert row["llm_call_count"] is None


def test_row_matches_the_schema_exactly():
    row = rb.coerce_row("e1", {"bug_id": "B1"})
    assert set(row) == {f.name for f in rb.build_schema()}


# ── timestamp parsing ────────────────────────────────────────────────────────


def test_run_ts_parsed_from_record_timestamp():
    ts = rb.parse_run_ts({"timestamp": "20260804T152240Z"}, "irrelevant")
    assert ts == "2026-08-04T15:22:40+00:00"


def test_run_ts_falls_back_to_the_entry_id_prefix():
    ts = rb.parse_run_ts({}, "20260804T152240Z_BUG-1_langgraph_m")
    assert ts == "2026-08-04T15:22:40+00:00"


def test_unparseable_timestamp_is_none_not_an_exception():
    """The row still loads; it just lands in the NULL partition. Dropping the
    run entirely would lose data over a cosmetic field."""
    assert rb.parse_run_ts({"timestamp": "nonsense"}, "also-nonsense") is None


# ── journal scanning ─────────────────────────────────────────────────────────


def _journal(tmp_path: Path, entries: dict[str, dict]) -> Path:
    root = tmp_path / "journal"
    root.mkdir()
    for name, record in entries.items():
        d = root / name
        d.mkdir()
        (d / "record.json").write_text(json.dumps(record), encoding="utf-8")
    return root


def test_scan_yields_directory_name_as_entry_id(tmp_path: Path):
    root = _journal(tmp_path, {"20260804T000000Z_B1_langgraph_m": {"bug_id": "B1"}})
    entries = list(rb.iter_journal_entries(root))
    assert entries == [("20260804T000000Z_B1_langgraph_m", {"bug_id": "B1"})]


def test_malformed_record_is_skipped_not_fatal(tmp_path: Path):
    """A half-written record from an in-flight fix must not abort a cron run."""
    root = _journal(tmp_path, {"good_1": {"bug_id": "B1"}})
    bad = root / "bad_1"
    bad.mkdir()
    (bad / "record.json").write_text("{not json", encoding="utf-8")

    names = [name for name, _ in rb.iter_journal_entries(root)]
    assert names == ["good_1"]


def test_directory_without_record_json_is_ignored(tmp_path: Path):
    root = _journal(tmp_path, {"good_1": {"bug_id": "B1"}})
    (root / "empty_dir").mkdir()
    assert len(list(rb.iter_journal_entries(root))) == 1


def test_missing_journal_dir_is_empty_not_an_error(tmp_path: Path):
    assert list(rb.iter_journal_entries(tmp_path / "nope")) == []


# ── export orchestration (BigQuery mocked) ───────────────────────────────────


class _FakeClient:
    """Records what the exporter asked BigQuery to do."""

    def __init__(self, existing_ids=()):
        self.project = "test-project"
        self._existing = list(existing_ids)
        self.loaded_rows: list[dict] = []
        self.load_configs: list = []
        self.created_tables: list = []

    def create_dataset(self, dataset, exists_ok=False):
        self.created_dataset = dataset
        return dataset

    def create_table(self, table, exists_ok=False):
        self.created_tables.append(table)
        return table

    def query(self, sql):
        rows = [{rb.ENTRY_ID_COLUMN: e} for e in self._existing]
        return type("Job", (), {"result": lambda self_: rows})()

    def load_table_from_json(self, rows, table_ref, job_config=None):
        self.loaded_rows.extend(rows)
        self.load_configs.append(job_config)
        return type("Job", (), {"result": lambda self_: None})()


@pytest.fixture
def fake_bq(monkeypatch):
    from google.cloud import bigquery

    holder = {}

    def _factory(existing_ids=()):
        client = _FakeClient(existing_ids)
        holder["client"] = client
        monkeypatch.setattr(bigquery, "Client", lambda project=None: client)
        return client

    return _factory


def test_export_loads_every_entry_into_an_empty_table(tmp_path, fake_bq):
    root = _journal(tmp_path, {
        "20260804T000000Z_B1_a_m": {"bug_id": "B1"},
        "20260804T000100Z_B2_a_m": {"bug_id": "B2"},
    })
    client = fake_bq()

    stats = rb.export(journal_dir=root)

    assert stats == {"scanned": 2, "skipped_existing": 0, "loaded": 2}
    assert {r["entry_id"] for r in client.loaded_rows} == {
        "20260804T000000Z_B1_a_m", "20260804T000100Z_B2_a_m"
    }


def test_export_is_idempotent(tmp_path, fake_bq):
    """The whole point: safe to put in cron. BigQuery would happily store the
    same run twice — nothing in the database prevents it."""
    root = _journal(tmp_path, {
        "20260804T000000Z_B1_a_m": {"bug_id": "B1"},
        "20260804T000100Z_B2_a_m": {"bug_id": "B2"},
    })
    client = fake_bq(existing_ids=["20260804T000000Z_B1_a_m"])

    stats = rb.export(journal_dir=root)

    assert stats == {"scanned": 2, "skipped_existing": 1, "loaded": 1}
    assert [r["entry_id"] for r in client.loaded_rows] == ["20260804T000100Z_B2_a_m"]


def test_export_skips_the_load_entirely_when_nothing_is_new(tmp_path, fake_bq):
    root = _journal(tmp_path, {"20260804T000000Z_B1_a_m": {"bug_id": "B1"}})
    client = fake_bq(existing_ids=["20260804T000000Z_B1_a_m"])

    stats = rb.export(journal_dir=root)

    assert stats["loaded"] == 0
    assert client.loaded_rows == []


def test_load_appends_and_never_truncates(tmp_path, fake_bq):
    """WRITE_TRUNCATE would make two exporting hosts take turns deleting each
    other's rows — and cross-host aggregation is the reason this exists."""
    from google.cloud import bigquery

    root = _journal(tmp_path, {"20260804T000000Z_B1_a_m": {"bug_id": "B1"}})
    client = fake_bq()

    rb.export(journal_dir=root)

    cfg = client.load_configs[0]
    assert cfg.write_disposition == bigquery.WriteDisposition.WRITE_APPEND


def test_load_allows_additive_schema_growth(tmp_path, fake_bq):
    """RunRecord evolves by adding fields (invariant #5); a load that rejected
    a new column would break the export the day someone adds telemetry."""
    from google.cloud import bigquery

    root = _journal(tmp_path, {"20260804T000000Z_B1_a_m": {"bug_id": "B1"}})
    client = fake_bq()

    rb.export(journal_dir=root)

    cfg = client.load_configs[0]
    assert bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION in cfg.schema_update_options


def test_table_is_day_partitioned_on_run_ts(tmp_path, fake_bq):
    root = _journal(tmp_path, {"20260804T000000Z_B1_a_m": {"bug_id": "B1"}})
    client = fake_bq()

    rb.export(journal_dir=root)

    table = client.created_tables[0]
    assert table.time_partitioning.field == rb.RUN_TS_COLUMN
    assert table.time_partitioning.type_ == "DAY"


def test_dry_run_touches_no_bigquery_client(tmp_path, monkeypatch):
    """--dry-run must work with no credentials and no network."""
    from google.cloud import bigquery

    def _explode(*a, **kw):
        raise AssertionError("dry-run must not construct a BigQuery client")

    monkeypatch.setattr(bigquery, "Client", _explode)
    root = _journal(tmp_path, {"20260804T000000Z_B1_a_m": {"bug_id": "B1"}})

    stats = rb.export(journal_dir=root, dry_run=True)

    assert stats == {"scanned": 1, "skipped_existing": 0, "loaded": 0}


def test_cli_dry_run_exits_zero(tmp_path, capsys):
    root = _journal(tmp_path, {"20260804T000000Z_B1_a_m": {"bug_id": "B1"}})
    rc = rb.main(["--journal-dir", str(root), "--dry-run"])
    assert rc == 0
    assert "scanned=1" in capsys.readouterr().out
