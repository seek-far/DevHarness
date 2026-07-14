"""Structural validation for infra/grafana/sdlcma_dashboard.json.

Can't test "does it render" without a real Grafana, but we CAN pin:
  * JSON parses
  * Required top-level fields present
  * Templated $datasource variable wired correctly
  * Every metric panel has a datasource ref + at least one target
  * No two non-row panels overlap on the grid (overlap = visually
    broken once imported)
  * Every PromQL target references a metric our code actually exports
    (catches metric-name typos before someone imports a broken
    dashboard)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

DASHBOARD_PATH = (
    Path(__file__).resolve().parents[1]
    / "infra" / "grafana" / "sdlcma_dashboard.json"
)


# All metrics our code currently exports. Keep this list in sync with
# the four metrics.py modules + runrecord_to_metrics.py. Adding a metric
# to the dashboard without adding it here trips the typo test.
KNOWN_METRICS = {
    # gateway/metrics.py
    "sdlcma_webhooks_received_total",
    "sdlcma_webhooks_forwarded_total",
    "sdlcma_gateway_handle_ms",
    "sdlcma_gateway_handle_ms_bucket",
    # orchestrator/metrics.py
    "sdlcma_workers_spawned_total",
    "sdlcma_worker_restarts_total",
    "sdlcma_bug_id_collisions_total",
    "sdlcma_dead_letter_total",
    "sdlcma_spawn_wallclock_ms",
    "sdlcma_spawn_wallclock_ms_bucket",
    "sdlcma_active_workers",
    "sdlcma_stream_pending",
    # llm_gateway/metrics.py
    "sdlcma_llm_cache_lookups_total",
    "sdlcma_llm_upstream_wallclock_ms",
    "sdlcma_llm_upstream_wallclock_ms_bucket",
    # runrecord_to_metrics.py
    "sdlcma_runs_total",
    "sdlcma_run_elapsed_seconds_total",
    "sdlcma_llm_tokens_total",
    # journal-derived (runrecord exporter) — cumulative, born-at-value
    "sdlcma_llm_cost_usd_total",
    # gateway-side, live per-call counters. The `upstream_`/`cache_` prefixes
    # keep these from colliding with the journal-derived family above: same
    # name, different label sets and different semantics would silently
    # double-count every token.
    "sdlcma_llm_upstream_cost_usd_total",
    "sdlcma_llm_cache_saved_usd_total",
    "sdlcma_llm_upstream_tokens_total",
    "sdlcma_llm_calls_total",
    "sdlcma_parse_trace_fallback_total",
    "sdlcma_reflection_fires_total",
    "sdlcma_runrecord_last_scan_timestamp",
}


@pytest.fixture(scope="module")
def dashboard():
    return json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))


def test_dashboard_is_valid_json(dashboard):
    """JSON parses — the most basic guarantee. Without this, Grafana
    import silently fails with a generic error."""
    assert isinstance(dashboard, dict)
    assert dashboard["title"]
    assert dashboard["uid"]
    assert "panels" in dashboard


def test_dashboard_has_templated_datasource(dashboard):
    """Hard-coded `Prometheus` in every panel makes the file
    non-portable across installs. Pin that we use a `$datasource`
    template variable so users can pick their own on import."""
    templating = dashboard.get("templating", {}).get("list", [])
    assert any(
        t.get("name") == "datasource" and t.get("type") == "datasource"
        for t in templating
    ), "missing $datasource template variable"


def test_non_row_panels_have_datasource_and_targets(dashboard):
    """Every real (non-row) panel must have a datasource ref AND at
    least one target. A panel without either renders blank in
    Grafana — silent failure that's hard to debug post-import."""
    for panel in dashboard["panels"]:
        if panel["type"] == "row":
            continue
        assert "datasource" in panel, f"panel {panel['id']} missing datasource"
        targets = panel.get("targets") or []
        assert targets, f"panel {panel['id']} has no targets"
        for t in targets:
            assert t.get("expr"), f"panel {panel['id']} target missing expr"


def test_no_panel_overlap_on_grid(dashboard):
    """Grafana places panels on a 24-column grid by gridPos {x, y, w, h}.
    Overlapping panels render on top of each other — visually broken
    on import. Walk panels in (y, x) order and assert each starts at
    or after the previous one's bottom edge for the column range it
    occupies."""
    panels = [p for p in dashboard["panels"] if "gridPos" in p]
    # Build an occupancy map keyed by (col_x, y_range).
    cells: dict[tuple[int, int], int] = {}  # (x, y) → panel_id
    for p in panels:
        gp = p["gridPos"]
        x0, y0, w, h = gp["x"], gp["y"], gp["w"], gp["h"]
        for x in range(x0, x0 + w):
            for y in range(y0, y0 + h):
                cell = (x, y)
                if cell in cells:
                    pytest.fail(
                        f"panel id={p['id']} overlaps panel id={cells[cell]} "
                        f"at cell {cell}"
                    )
                cells[cell] = p["id"]


def test_every_target_references_a_known_metric(dashboard):
    """Catches metric-name typos before someone imports the dashboard
    and sees blank panels. The KNOWN_METRICS set is the authoritative
    list of what our code actually exports; if a panel references a
    typo'd name, we want the test to fail HERE not silently in
    production."""
    metric_re = re.compile(r"\bsdlcma_[A-Za-z0-9_]+")
    for panel in dashboard["panels"]:
        if panel["type"] == "row":
            continue
        for t in panel.get("targets") or []:
            expr = t.get("expr", "")
            for match in metric_re.findall(expr):
                assert match in KNOWN_METRICS, (
                    f"panel id={panel['id']} references unknown metric "
                    f"{match!r}; either fix the name or add it to "
                    f"KNOWN_METRICS"
                )


def test_health_panels_have_thresholds(dashboard):
    """Health-row panels (collisions / dead-letter / restarts /
    scan age) only convey their alert intent via threshold colours.
    Without a threshold config they render uniform green — the operator
    can't visually tell "should be 0" from "any value is fine"."""
    health_panel_ids = {2, 3, 4, 5}   # see dashboard JSON
    for panel in dashboard["panels"]:
        if panel["id"] not in health_panel_ids:
            continue
        steps = (panel.get("fieldConfig", {})
                       .get("defaults", {})
                       .get("thresholds", {})
                       .get("steps", []))
        assert len(steps) >= 2, (
            f"health panel id={panel['id']} missing threshold steps"
        )


def test_refresh_and_time_defaults_set(dashboard):
    """Sane defaults so an import lands on a useful initial view."""
    assert dashboard.get("refresh"), "no refresh interval set"
    t = dashboard.get("time", {})
    assert t.get("from") and t.get("to"), "no default time window"


def test_uid_and_title_for_provisioning():
    """uid is what file-based provisioning matches on for idempotent
    updates. Without it, re-import creates duplicates."""
    data = json.loads(DASHBOARD_PATH.read_text())
    assert data.get("uid") == "sdlcma-main"


# Metrics rendered by tools/runrecord_to_metrics.py — the runrecord-exporter
# recomputes these cumulative totals from the journal on every scrape. A
# series is BORN at its full value when its first record appears and then
# stays flat, so rate()/increase() over any window that doesn't straddle that
# single step return ~0 (incident 2026-05-31: Tokens-per-fix and Fix-rate read
# 0 after a real fix). Use absolute sums / ratios for these; rate/increase is
# only meaningful for the in-process event-stream counters (webhooks, dead
# letter, cache lookups, …) which have a real 0 baseline and change over time.
_JOURNAL_DERIVED_METRICS = {
    "sdlcma_runs_total",
    "sdlcma_run_elapsed_seconds_total",
    "sdlcma_llm_tokens_total",
    "sdlcma_llm_cost_usd_total",
    "sdlcma_llm_calls_total",
    "sdlcma_parse_trace_fallback_total",
    "sdlcma_reflection_fires_total",
}
# NOT journal-derived, despite the similar names: the gateway's
# sdlcma_llm_upstream_* / sdlcma_llm_cache_saved_* counters are true in-process
# monotonic per-call counters with a real 0 baseline, so rate() over them IS
# meaningful. The prefixes exist precisely to keep the two families apart.


def test_journal_metrics_not_wrapped_in_rate_or_increase(dashboard):
    """journal-recompute counters must not be inside rate()/increase() — those
    collapse to ~0 for the sparse, born-at-value series the exporter emits."""
    # Match rate(...) / increase(...) call bodies and check for a journal metric.
    call_re = re.compile(r"\b(?:rate|increase)\s*\(([^)]*)\)")
    for panel in dashboard["panels"]:
        if panel["type"] == "row":
            continue
        for t in panel.get("targets") or []:
            for body in call_re.findall(t.get("expr", "")):
                bad = _JOURNAL_DERIVED_METRICS.intersection(
                    re.findall(r"\bsdlcma_[A-Za-z0-9_]+", body)
                )
                assert not bad, (
                    f"panel id={panel['id']} wraps journal-recompute metric(s) "
                    f"{sorted(bad)} in rate()/increase(); these read ~0 for "
                    f"sparse fixes — use absolute sum()/ratio instead"
                )


def test_chart_copy_matches_canonical():
    """The chart ships a mirror of this file at infra/helm/sdlcma/dashboards/
    so Helm's `Files.Get` can embed it into a ConfigMap (helm package can
    only ship files inside the chart dir). The two copies MUST be
    byte-identical — drift means K8s users get a stale dashboard while
    bare-host users see the current one (or vice versa). This test makes
    a forgotten copy fail at PR review, not at deploy."""
    chart_copy = (
        Path(__file__).resolve().parents[1]
        / "infra" / "helm" / "sdlcma" / "dashboards" / "sdlcma_dashboard.json"
    )
    assert chart_copy.is_file(), (
        f"chart copy missing at {chart_copy} — run "
        f"`cp infra/grafana/sdlcma_dashboard.json {chart_copy}` after every "
        f"dashboard edit"
    )
    assert chart_copy.read_bytes() == DASHBOARD_PATH.read_bytes(), (
        f"chart copy {chart_copy} differs from canonical {DASHBOARD_PATH} "
        f"— re-run `cp infra/grafana/sdlcma_dashboard.json {chart_copy}` "
        f"to resync (the chart's grafana-dashboard-cm.yaml reads the chart "
        f"copy via Files.Get)"
    )
