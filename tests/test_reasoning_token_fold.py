"""Thinking tokens must reach RunRecord's token counters, not just the bill.

Provenance (2026-08-04, first live Vertex run): `llm_gateway/cost.py` folded
reasoning into billable output, so `RunRecord.total_cost_usd` was right — but
`bf_worker/services/budget.py::extract_token_usage` still read LangChain's raw
`output_tokens`, so the SAME record reported

    total_completion_tokens   334      recorded
    back-solved from cost    1153      actually billed

i.e. 819 thinking tokens (71 % of the output) invisible to every tokens-per-fix
analysis. This file pins the worker half and, critically, pins that the two
`_fold_reasoning` implementations cannot drift apart.

Why there ARE two: the gateway image and the worker image never contain each
other's source (tests/test_image_dependencies.py), the same constraint that
gives azure_auth.py a twin. Duplication is the cheaper trade; silent divergence
is the risk, so test_parity_* below is the compensating control.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from services.budget import (  # noqa: E402
    _fold_reasoning as worker_fold,
    _reasoning_count,
    extract_token_usage,
)
from llm_gateway.cost import _fold_reasoning as gateway_fold  # noqa: E402


def _msg_usage_metadata(inp, out, reasoning=None, total=None):
    """LangChain's preferred shape (ChatOpenAI populates usage_metadata)."""
    meta = {"input_tokens": inp, "output_tokens": out}
    if total is not None:
        meta["total_tokens"] = total
    if reasoning is not None:
        meta["output_token_details"] = {"reasoning": reasoning}
    return SimpleNamespace(usage_metadata=meta, response_metadata={})


def _msg_response_metadata(inp, out, reasoning=None, total=None):
    """The older path — raw OpenAI usage dict under response_metadata."""
    usage = {"prompt_tokens": inp, "completion_tokens": out}
    if total is not None:
        usage["total_tokens"] = total
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return SimpleNamespace(usage_metadata=None, response_metadata={"token_usage": usage})


# ── the Vertex dialect, through both message shapes ──────────────────────────


@pytest.mark.parametrize("build", [_msg_usage_metadata, _msg_response_metadata])
def test_vertex_dialect_folds_thinking_into_output(build):
    """total == prompt + completion + reasoning ⇒ reasoning is charged on top."""
    inp, out = extract_token_usage(build(8, 10, reasoning=27, total=45))
    assert (inp, out) == (8, 37)


@pytest.mark.parametrize("build", [_msg_usage_metadata, _msg_response_metadata])
def test_openai_dialect_does_not_double_count(build):
    """total == prompt + completion ⇒ completion already contains reasoning."""
    inp, out = extract_token_usage(build(100, 50, reasoning=30, total=150))
    assert (inp, out) == (100, 50)


def test_the_live_run_numbers():
    """The measurement that motivated the fix, as a regression.

    2249 prompt / 334 visible completion / 819 thinking → 1153 billable, which
    is what $0.003558 at $0.30+$2.50 per 1M prices out to.
    """
    inp, out = extract_token_usage(
        _msg_usage_metadata(2249, 334, reasoning=819, total=2249 + 334 + 819)
    )
    assert (inp, out) == (2249, 1153)
    assert pytest.approx(2249 * 0.30 / 1e6 + out * 2.50 / 1e6, rel=1e-3) == 0.003558


# ── conservative fallbacks ───────────────────────────────────────────────────


@pytest.mark.parametrize("build", [_msg_usage_metadata, _msg_response_metadata])
def test_missing_total_keeps_the_raw_reading(build):
    inp, out = extract_token_usage(build(8, 10, reasoning=27, total=None))
    assert (inp, out) == (8, 10)


@pytest.mark.parametrize("build", [_msg_usage_metadata, _msg_response_metadata])
def test_incoherent_total_keeps_the_raw_reading(build):
    """Matches neither dialect ⇒ we don't understand this backend. Under-report
    rather than invent tokens."""
    inp, out = extract_token_usage(build(8, 10, reasoning=27, total=999))
    assert (inp, out) == (8, 10)


@pytest.mark.parametrize("build", [_msg_usage_metadata, _msg_response_metadata])
def test_no_reasoning_details_is_unchanged(build):
    """Every non-thinking backend — the overwhelmingly common case."""
    assert extract_token_usage(build(100, 50, total=150)) == (100, 50)


@pytest.mark.parametrize("build", [_msg_usage_metadata, _msg_response_metadata])
def test_zero_reasoning_is_a_noop(build):
    assert extract_token_usage(build(100, 50, reasoning=0, total=150)) == (100, 50)


def test_no_usage_at_all_still_returns_zeros():
    msg = SimpleNamespace(usage_metadata=None, response_metadata={})
    assert extract_token_usage(msg) == (0, 0)


def test_langchain_synthesised_total_lands_on_the_safe_branch():
    """langchain-openai fills total_tokens = input + output when the backend
    omits it (base.py::_create_usage_metadata). That equals the OpenAI identity
    by construction, so the fold must not fire off a synthesised number."""
    inp, out = extract_token_usage(_msg_usage_metadata(8, 10, reasoning=27, total=18))
    assert (inp, out) == (8, 10)


# ── the service-tier key prefix ──────────────────────────────────────────────


def test_service_tier_prefixed_reasoning_key_is_found():
    """langchain-openai emits `priority_reasoning` / `flex_reasoning` when an
    OpenAI service tier is set."""
    assert _reasoning_count({"priority_reasoning": 27}) == 27
    assert _reasoning_count({"flex_reasoning": 5, "audio": None}) == 5
    assert _reasoning_count({"reasoning": 12}) == 12
    assert _reasoning_count({"audio": 3}) == 0
    assert _reasoning_count(None) == 0
    assert _reasoning_count({"reasoning": None}) == 0
    assert _reasoning_count({"reasoning": "nonsense"}) == 0


# ── the two implementations must not drift ───────────────────────────────────


_FOLD_CASES = [
    (8, 10, 27, 45),          # vertex dialect
    (100, 50, 30, 150),       # openai dialect
    (8, 10, 27, None),        # no total
    (8, 10, 27, 999),         # incoherent
    (100, 50, 0, 150),        # no reasoning
    (0, 0, 0, 0),             # empty
    (21, 8, 66, 95),          # live probe #2
    (2249, 334, 819, 3402),   # live run
    (8, 10, 27, True),        # bool must not read as int 1
    (8, 10, 27, "45"),        # string total
]


@pytest.mark.parametrize("prompt,completion,reasoning,total", _FOLD_CASES)
def test_parity_between_worker_and_gateway_fold(prompt, completion, reasoning, total):
    """The compensating control for having two copies. If someone fixes a bug in
    one and not the other, cost and token counts silently disagree again — which
    is the exact failure this whole file exists to prevent."""
    assert worker_fold(prompt, completion, reasoning, total) == gateway_fold(
        prompt, completion, reasoning, total
    )
