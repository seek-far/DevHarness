from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "trial" / "swebench_background_search.py"


def load_module():
    spec = importlib.util.spec_from_file_location("swebench_background_search", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_instances():
    return [
        {"instance_id": "sympy__sympy-3", "problem_statement": "c"},
        {"instance_id": "django__django-1", "problem_statement": "a"},
        {"instance_id": "astropy__astropy-2", "problem_statement": "b"},
    ]


def test_select_instances_accepts_ids_in_requested_order():
    mod = load_module()

    selected = mod.select_instances(
        fake_instances(),
        ids=["astropy__astropy-2", "django__django-1"],
        slice_spec="",
        all_instances=False,
    )

    assert [item["instance_id"] for item in selected] == ["astropy__astropy-2", "django__django-1"]


def test_select_instances_supports_slice_over_sorted_ids():
    mod = load_module()

    selected = mod.select_instances(fake_instances(), ids=[], slice_spec="1:3", all_instances=False)

    assert [item["instance_id"] for item in selected] == ["django__django-1", "sympy__sympy-3"]


def test_select_instances_requires_exactly_one_selector():
    mod = load_module()

    with pytest.raises(ValueError):
        mod.select_instances(fake_instances(), ids=[], slice_spec="", all_instances=False)

    with pytest.raises(ValueError):
        mod.select_instances(fake_instances(), ids=["django__django-1"], slice_spec="0:1", all_instances=False)


def test_coerce_search_plan_parses_fenced_json():
    mod = load_module()

    plan = mod.coerce_search_plan(
        """```json
        {"queries":[{"query":"Django form field validation order","rationale":"API behavior"}]}
        ```""",
        max_queries=3,
    )

    assert plan["queries"] == [
        {
            "query": "Django form field validation order",
            "rationale": "API behavior",
            "knowledge_type": "",
        }
    ]


def test_filter_search_plan_rejects_code_related_queries():
    mod = load_module()
    instance = {"instance_id": "django__django-11532", "repo": "django/django"}
    plan = {
        "queries": [
            {"query": "Django QuerySet aggregation behavior", "rationale": "background"},
            {"query": "github django django issue 11532 patch", "rationale": "specific fix"},
            {"query": "django/db/models/query.py implementation", "rationale": "source"},
        ]
    }

    accepted, rejected = mod.filter_search_plan(plan, instance)

    assert [item["query"] for item in accepted] == ["Django QuerySet aggregation behavior"]
    assert [item["rejected_reason"] for item in rejected] == ["instance issue number", "code artifact"]


def test_duckduckgo_html_parser_extracts_results_and_unwraps_urls():
    mod = load_module()
    parser = mod.DuckDuckGoHTMLParser()

    parser.feed(
        """
        <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.djangoproject.com%2Fen%2F5.0%2Ftopics%2Fi18n%2Ftranslation%2F">
          Django translation docs
        </a>
        <a class="result__snippet">URL internationalization behavior.</a>
        """
    )

    assert parser.results == [
        {
            "title": "Django translation docs",
            "url": "https://docs.djangoproject.com/en/5.0/topics/i18n/translation/",
            "snippet": "URL internationalization behavior.",
        }
    ]


def test_choose_provider_uses_duckduckgo_html_without_api_keys(monkeypatch):
    mod = load_module()
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("SERPER_API_KEY", raising=False)

    assert mod.choose_provider("auto") == "duckduckgo_html"


def test_result_url_block_reason_blocks_code_hosts():
    mod = load_module()

    assert mod.result_url_block_reason("https://github.com/django/django") == "code-related host"
    assert mod.result_url_block_reason("https://docs.djangoproject.com/en/stable/") is None
