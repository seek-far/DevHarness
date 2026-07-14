"""Cost tracking + the BF_MAX_COST_USD hard ceiling.

The ceiling is the only cap that bounds *spend* rather than *work*. The existing
token cap cannot substitute: price per token varies by two orders of magnitude
across backends (self-hosted is free; a reasoning model bills its hidden
reasoning tokens as output, so the same token count can cost 10x more).

The invariant threaded through all of this: **an unknown cost is None, never
0.0**. A fabricated $0.00 in a cost dashboard reads as "this was free", which is
a lie; a blank reads as "go find out", which is the truth.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from services import pricing  # noqa: E402
from services.budget import RunBudget  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    pricing.reset_cache()
    yield
    pricing.reset_cache()


def _table(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "pricing.yaml"
    p.write_text(body, encoding="utf-8")
    return p


# ── pricing table ────────────────────────────────────────────────────────────


def test_known_model_is_priced(tmp_path: Path):
    pricing.load_table(_table(tmp_path, """
models:
  gpt-5-mini:
    input_per_1m: 0.25
    output_per_1m: 2.00
    cached_input_per_1m: 0.025
"""))
    price = pricing.load_table(_table(tmp_path, """
models:
  gpt-5-mini:
    input_per_1m: 0.25
    output_per_1m: 2.00
    cached_input_per_1m: 0.025
"""))["gpt-5-mini"]
    # The real F01-against-Azure numbers.
    cost = price.cost_usd(prompt=4553, completion=2150, cached=2816)
    expected = (4553 - 2816) * 0.25 / 1e6 + 2816 * 0.025 / 1e6 + 2150 * 2.00 / 1e6
    assert cost == pytest.approx(expected)


def test_unknown_model_is_none_not_zero(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BF_PRICING_FILE", str(_table(tmp_path, "models: {}\n")))
    pricing.reset_cache()
    assert pricing.cost_for("some-self-hosted-qwen", 1000, 100) is None


def test_missing_pricing_file_is_not_fatal(tmp_path: Path, monkeypatch):
    """A run must not die because nobody wrote a price list."""
    monkeypatch.setenv("BF_PRICING_FILE", str(tmp_path / "nope.yaml"))
    pricing.reset_cache()
    assert pricing.cost_for("gpt-5-mini", 1000, 100) is None


def test_cached_tokens_discount_not_surcharge(tmp_path: Path, monkeypatch):
    """cached_tokens is a SUBSET of prompt_tokens — it must reduce the bill."""
    monkeypatch.setenv("BF_PRICING_FILE", str(_table(tmp_path, """
models:
  m:
    input_per_1m: 1.0
    output_per_1m: 0.0
    cached_input_per_1m: 0.1
""")))
    pricing.reset_cache()
    none_cached = pricing.cost_for("m", 1000, 0, 0)
    all_cached = pricing.cost_for("m", 1000, 0, 1000)
    assert all_cached == pytest.approx(none_cached / 10)


# ── budget: the hard ceiling ─────────────────────────────────────────────────


def test_cost_cap_off_by_default():
    # Raise the other caps so this test observes the COST dimension only.
    b = RunBudget(max_calls=10_000, max_tokens=10**9, max_wallclock_s=10_000)
    assert b.max_cost_usd is None
    b.record_call(1000, 100, cost_usd=999.0)
    assert b.check() is None, "no cap configured → cost must never exhaust the run"


def test_cost_cap_trips_and_names_the_dollar_figure():
    b = RunBudget(max_cost_usd=0.01)
    b.record_call(1000, 100, cost_usd=0.004)
    assert b.check() is None
    b.record_call(1000, 100, cost_usd=0.007)   # total 0.011 > 0.01
    reason = b.check()
    assert reason is not None
    assert "cost limit reached" in reason
    assert "0.011" in reason and "0.010" in reason


def test_unpriced_calls_leave_cost_none_and_never_trip_the_cap():
    """Self-hosted backends report no price. That must not be read as free
    (which would silently disable the cap) NOR as infinite (which would abort
    every run)."""
    b = RunBudget(
        max_cost_usd=0.01, max_calls=10_000, max_tokens=10**9, max_wallclock_s=10_000
    )
    for _ in range(50):
        b.record_call(10_000, 1_000, cost_usd=None)
    assert b.cost_usd is None
    assert b.check() is None


def test_mixed_priced_and_unpriced_accumulates_only_the_known_part():
    b = RunBudget(max_cost_usd=1.0)
    b.record_call(100, 10, cost_usd=None)
    b.record_call(100, 10, cost_usd=0.25)
    b.record_call(100, 10, cost_usd=None)
    assert b.cost_usd == pytest.approx(0.25)


def test_first_tripped_cap_is_the_one_reported():
    """check() caches the first reason so the run record shows what actually
    ended the run, not whichever cap crossed last."""
    b = RunBudget(max_calls=2, max_cost_usd=0.001)
    b.record_call(10, 1, cost_usd=0.5)   # cost already way over
    b.record_call(10, 1, cost_usd=0.5)   # …and now calls is at the cap too
    reason = b.check()
    assert "call limit" in reason
    assert b.check() == reason


def test_to_dict_carries_cost_and_cap():
    b = RunBudget(max_cost_usd=0.05)
    b.record_call(1000, 100, cost_usd=0.0123456789)
    d = b.to_dict()
    assert d["max_cost_usd"] == 0.05
    assert d["cost_usd"] == pytest.approx(0.012346, abs=1e-6)


def test_to_dict_cost_is_none_when_unpriced():
    d = RunBudget().to_dict()
    assert d["cost_usd"] is None
    assert d["max_cost_usd"] is None


# ── BF_MAX_COST_USD parsing ──────────────────────────────────────────────────


def test_env_cap_parsing(monkeypatch):
    from agents.langgraph_agent import _max_cost_usd_from_env

    monkeypatch.delenv("BF_MAX_COST_USD", raising=False)
    assert _max_cost_usd_from_env() is None

    monkeypatch.setenv("BF_MAX_COST_USD", "0.50")
    assert _max_cost_usd_from_env() == pytest.approx(0.50)


@pytest.mark.parametrize("bad", ["", "  ", "abc", "0", "-1"])
def test_malformed_env_cap_means_no_cap_not_a_zero_cap(monkeypatch, bad):
    """A cap of 0.0 would abort every run before its first LLM call — that looks
    like a total outage, and a typo must not cause one."""
    from agents.langgraph_agent import _max_cost_usd_from_env

    monkeypatch.setenv("BF_MAX_COST_USD", bad)
    assert _max_cost_usd_from_env() is None


# ── lint: a zero-priced entry is a lie, not a placeholder ────────────────────
#
# `None` (absent) and `0.0` (present, priced at zero) are NOT interchangeable:
#   absent  → total_cost_usd stays None      → "we don't know what this cost"
#   0.0     → total_cost_usd = 0.0,
#             cost_source = "local_pricing"  → "we priced it, and it was free"
# The second is a confident lie, and it renders as a real $0.00 on the cost
# dashboard. A zero-priced entry shipped in configs/pricing.yaml as a
# "not yet priced" placeholder (caught 2026-07-14) — hence this guard.

_REPO = Path(__file__).resolve().parents[1]


def _shipped_pricing_models() -> dict:
    import yaml

    data = yaml.safe_load((_REPO / "configs" / "pricing.yaml").read_text(encoding="utf-8"))
    return (data or {}).get("models") or {}


def test_shipped_pricing_table_has_no_zero_priced_placeholders():
    for model, raw in _shipped_pricing_models().items():
        rates = [raw.get("input_per_1m") or 0, raw.get("output_per_1m") or 0]
        assert any(r > 0 for r in rates), (
            f"configs/pricing.yaml prices {model!r} at zero. That does not mean "
            f"'unpriced' — it means 'we priced this and it was free', so the run "
            f"reports total_cost_usd=0.0 with cost_source='local_pricing'. To say "
            f"'unknown', REMOVE the entry: absent -> None -> honest."
        )


def test_shipped_gateway_configs_have_no_zero_priced_backends():
    """Same rule on the other pricing surface. A gateway backend either declares
    real prices or declares none — never zeros."""
    import yaml

    for cfg_path in sorted((_REPO / "configs" / "llm_gateway").glob("*.yaml")):
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        for backend in data.get("backends") or []:
            pricing = backend.get("pricing")
            if pricing is None:
                continue  # absent = "unpriced" = honest
            rates = [pricing.get("input_per_1m") or 0, pricing.get("output_per_1m") or 0]
            assert any(r > 0 for r in rates), (
                f"{cfg_path.name}: backend {backend.get('name')!r} declares a "
                f"pricing block with zero rates. Drop the block entirely to mean "
                f"'unpriced' — zeros claim the calls are free."
            )
