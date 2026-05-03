"""Journal directory naming + RunRecord.llm_model wiring.

The journal directory layout is part of the user-visible interface (people
read it via `ls`, the eval CLI globs over it). Adding the LLM model to the
name lets you tell at a glance which model produced a given run, which is
critical because model is a primary driver of bug-fix performance.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from agents.run_record import RunRecord  # noqa: E402
from journal import JournalWriter, _model_slug  # noqa: E402


# ── _model_slug ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "model, expected",
    [
        (None, ""),
        ("", ""),
        ("qwen-coder-plus", "qwen-coder-plus"),
        ("qwen2.5-coder-7b", "qwen2.5-coder-7b"),
        ("mistralai/Mistral-7B-Instruct", "mistralai-Mistral-7B-Instruct"),
        ("a/b/c/d", "a-b-c-d"),
        ("model with spaces", "model-with-spaces"),
        ("ns:tag", "ns-tag"),
    ],
)
def test_model_slug_basic(model, expected):
    assert _model_slug(model) == expected


def test_model_slug_strips_outer_dashes():
    assert _model_slug("///bad///") == "bad"


def test_model_slug_caps_length():
    very_long = "x" * 200
    slug = _model_slug(very_long)
    assert 0 < len(slug) <= 60


# ── JournalWriter directory naming ───────────────────────────────────────────


def _record(**overrides) -> RunRecord:
    base = dict(
        schema_version="1",
        agent_name="langgraph",
        bug_id="BUG-1",
        outcome="fixed",
        timestamp="20260503T120000Z",
        iterations=1,
    )
    base.update(overrides)
    return RunRecord(**base)


def test_no_model_falls_back_to_legacy_layout(tmp_path):
    writer = JournalWriter(journal_dir=tmp_path)
    record = _record()  # llm_model defaults to None
    out = writer.write(record, final_state={})
    assert out is not None
    assert out.name == "20260503T120000Z_BUG-1_langgraph"
    assert out.exists()


def test_model_appears_as_suffix(tmp_path):
    writer = JournalWriter(journal_dir=tmp_path)
    record = _record(llm_model="qwen-coder-plus")
    out = writer.write(record, final_state={})
    assert out is not None
    assert out.name == "20260503T120000Z_BUG-1_langgraph_qwen-coder-plus"


def test_slash_in_model_does_not_create_subdir(tmp_path):
    writer = JournalWriter(journal_dir=tmp_path)
    record = _record(llm_model="mistralai/Mistral-7B")
    out = writer.write(record, final_state={})
    assert out is not None
    # one level deep — no extra directory because of the slash
    assert out.parent == tmp_path
    assert "mistralai-Mistral-7B" in out.name


def test_record_json_contains_llm_model(tmp_path):
    writer = JournalWriter(journal_dir=tmp_path)
    record = _record(llm_model="qwen-coder-plus")
    out = writer.write(record, final_state={})
    payload = json.loads((out / "record.json").read_text(encoding="utf-8"))
    assert payload["llm_model"] == "qwen-coder-plus"


def test_record_json_keeps_field_when_absent(tmp_path):
    writer = JournalWriter(journal_dir=tmp_path)
    record = _record()  # no model
    out = writer.write(record, final_state={})
    payload = json.loads((out / "record.json").read_text(encoding="utf-8"))
    # field is in the schema but value is null
    assert "llm_model" in payload
    assert payload["llm_model"] is None


# ── RunRecord round-trip ─────────────────────────────────────────────────────


def test_runrecord_from_outputs_threads_llm_model():
    record = RunRecord.from_outputs(
        agent_name="langgraph",
        bug_id="BUG-X",
        outcome="fixed",
        error=None,
        iterations=0,
        final_state=None,
        llm_model="qwen-coder-plus",
    )
    assert record.llm_model == "qwen-coder-plus"


def test_runrecord_from_dict_round_trip():
    record = RunRecord.from_outputs(
        agent_name="langgraph",
        bug_id="BUG-X",
        outcome="fixed",
        error=None,
        iterations=0,
        final_state=None,
        llm_model="qwen-coder-plus",
    )
    d = record.to_dict()
    again = RunRecord.from_dict(d)
    assert again.llm_model == "qwen-coder-plus"


def test_runrecord_from_dict_legacy_record_without_field():
    # An old journal entry written before this field existed must still load.
    legacy = {
        "schema_version":   "1",
        "agent_name":       "langgraph",
        "bug_id":           "BUG-OLD",
        "outcome":          "fixed",
        "timestamp":        "20260101T000000Z",
    }
    record = RunRecord.from_dict(legacy)
    assert record.llm_model is None
