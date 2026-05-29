"""Tests for cache-key normalisation (`derive_key(body, normalize_content=True)`).

Each per-run volatile pattern in a GitLab CI trace (timestamps, runner
ID, Docker SHA, etc.) is exercised individually — proving the pattern
matches what it's supposed to AND doesn't over-match neighbouring text.

The headline integration test feeds two realistic CI traces that differ
ONLY in machinery metadata and asserts they hash to the same cache key
when the flag is on (and to different keys when it's off, preserving
the pre-flag default).

Motivation: when stress-testing the same fixture across multiple
GitLab pipeline triggers, the worker sends a NEW trace each time
(GitLab stamps every line with a fresh timestamp). Without
normalisation the cache key drifts every run and the hit rate
collapses to 0 %, as observed live on 2026-05-29.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm_gateway.keying import (
    _normalize_volatile_content,
    derive_key,
)


# ── per-pattern unit tests ──────────────────────────────────────────────────


def test_iso8601_timestamp_collapsed():
    """Per-line timestamp with microsecond precision and trailing Z —
    the most common shape in GitLab runner logs."""
    a = "2026-05-28T22:02:12.634090Z 00O Running with gitlab-runner"
    b = "2026-05-29T03:14:09.999999Z 00O Running with gitlab-runner"
    assert _normalize_volatile_content(a) == _normalize_volatile_content(b)
    # Sanity: the placeholder appears where the timestamp was.
    assert "<TS>" in _normalize_volatile_content(a)


def test_iso8601_without_fractional_seconds_also_matches():
    """The pattern allows the .fffff segment to be absent — same shape
    GitLab uses for some webhook payload fields."""
    out = _normalize_volatile_content("at 2026-05-28T22:02:12Z something")
    assert "<TS>" in out
    assert "2026-05-28T22:02:12Z" not in out


def test_section_marker_epoch_collapsed():
    """section_start:<unix-secs>:<name> — collapses only the epoch, the
    section name stays so the trace structure is still readable."""
    a = "section_start:1780005732:prepare_executor"
    b = "section_start:1780009999:prepare_executor"
    assert _normalize_volatile_content(a) == _normalize_volatile_content(b)
    assert _normalize_volatile_content(a) == "section_start:<EPOCH>:prepare_executor"
    assert _normalize_volatile_content("section_end:1780005732:prepare_executor") \
        == "section_end:<EPOCH>:prepare_executor"


def test_docker_sha256_collapsed():
    """Image digests: `sha256:` followed by 64 hex chars."""
    a = "Using docker image sha256:a3ab0b966bc4e91546a033e22093cb840908979487a9fc0e6e38295747e49ac0 for python"
    b = "Using docker image sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff for python"
    assert _normalize_volatile_content(a) == _normalize_volatile_content(b)
    assert "sha256:<SHA256>" in _normalize_volatile_content(a)


def test_runner_id_collapsed():
    """Runner instance ID encodes project number + concurrent slot."""
    a = "Running on runner-sy3kr6y1-project-2-concurrent-0 via minus"
    b = "Running on runner-aa9bxxxx-project-17-concurrent-3 via minus"
    assert _normalize_volatile_content(a) == _normalize_volatile_content(b)
    assert "runner-<ID>" in _normalize_volatile_content(a)


def test_runner_system_id_collapsed():
    a = "system ID: s_d89ad77e3cdd"
    b = "system ID: s_aabbccddeeff"
    assert _normalize_volatile_content(a) == _normalize_volatile_content(b)
    assert "<ID>" in _normalize_volatile_content(a)


def test_gitaly_correlation_id_collapsed():
    """Crockford-base32 ULID — case-insensitive in the prefix label."""
    a = "Gitaly correlation ID: 01KSR9QZV42TYJC8WKGA4EYHJV"
    b = "Gitaly correlation ID: 01ABCDEFGHJKMNPQRSTVWXYZ12"
    assert _normalize_volatile_content(a) == _normalize_volatile_content(b)


def test_detached_head_commit_sha_collapsed():
    """Detached-HEAD commit is anchored by the surrounding phrase so we
    don't accidentally strip hex strings elsewhere (e.g. inside source
    code that legitimately contains 0xDEADBEEF)."""
    a = "Checking out 978e40b2 as detached HEAD (ref is main)"
    b = "Checking out 1eb7c9c5 as detached HEAD (ref is main)"
    assert _normalize_volatile_content(a) == _normalize_volatile_content(b)
    assert "<COMMIT>" in _normalize_volatile_content(a)


def test_hex_string_NOT_in_detached_head_context_is_preserved():
    """A 7-40 char hex string in unrelated context must NOT be stripped
    — the surrounding code might legitimately contain a hash literal."""
    text = "assert h == 'a3ab0b96'"
    assert _normalize_volatile_content(text) == text


def test_pytest_duration_collapsed_single_outcome():
    a = "1 passed in 0.05s"
    b = "1 passed in 0.42s"
    assert _normalize_volatile_content(a) == _normalize_volatile_content(b)
    assert "<DUR>" in _normalize_volatile_content(a)


def test_pytest_duration_collapsed_multi_outcome():
    """Real failure summary has multiple comma-separated outcomes — the
    PRIMARY drift source observed live: identical fixture, same failure,
    only the `in N.NNs` portion varied between runs (host-load jitter
    on pytest's own wall-clock)."""
    a = "2 failed, 2 passed in 0.03s"
    b = "2 failed, 2 passed in 0.06s"
    assert _normalize_volatile_content(a) == _normalize_volatile_content(b)


def test_pytest_outcome_counts_NOT_collapsed():
    """The outcome counts must stay — `2 failed` vs `3 failed` is a
    semantically different result and SHOULD produce a different cache
    key. Pin that the normalisation didn't accidentally over-reach."""
    a = "2 failed, 2 passed in 0.03s"
    b = "3 failed, 1 passed in 0.03s"
    assert _normalize_volatile_content(a) != _normalize_volatile_content(b)


def test_pytest_duration_pattern_does_not_match_arbitrary_text():
    """`5 apples in 0.5s` looks superficially similar but is not pytest
    output; the word "apples" isn't a pytest outcome. Confirm the
    pattern's pytest-outcome word list keeps it strict."""
    text = "I have 5 apples in 0.5s"
    assert _normalize_volatile_content(text) == text


def test_bug_id_collapsed_in_path():
    """The orchestrator-minted `<date>-<time>_<dec>_<urandom4>` bug_id
    leaks into retry_feedback paths and error messages. Stripping it
    is what makes RETRY cache hits possible across stress-test bursts
    (where each webhook trip mints a fresh bug_id)."""
    a = "/tmp/dh_repo/2026_05_29-00_37_02_1_b10f/palindrome.py"
    b = "/tmp/dh_repo/2026_05_29-12_11_45_3_aaaa/palindrome.py"
    assert _normalize_volatile_content(a) == _normalize_volatile_content(b)
    assert "<BUG_ID>" in _normalize_volatile_content(a)


def test_bug_id_no_tail_also_matches():
    """Pre-Item-1 bug_ids (no urandom tail) appear in any historical
    test data — the regex must accept that shape too."""
    a = "/tmp/dh_repo/2026_05_29-00_37_02_1/palindrome.py"
    b = "/tmp/dh_repo/2026_05_29-12_11_45_3/palindrome.py"
    assert _normalize_volatile_content(a) == _normalize_volatile_content(b)


def test_bug_id_pattern_is_tight_does_not_match_random_underscored_words():
    """The regex anchors on the exact `YYYY_MM_DD-HH_MM_SS_d[_xxxx]`
    shape so it doesn't accidentally strip arbitrary underscored
    identifiers."""
    text = "my_random_2026_var = 5"
    assert _normalize_volatile_content(text) == text


# ── property: deterministic test content survives ───────────────────────────


def test_test_failure_section_unchanged():
    """The pytest failure portion is the SEMANTIC content we want the
    LLM to reason about. It must pass through normalisation unchanged so
    cached responses still match the request after normalising."""
    failure = (
        "FAILED test_format_ids.py::test_int_ids - TypeError: sequence item 0: "
        "expected str instance, int found\n"
        "    def format_ids(ids) -> str:\n"
        ">       return ', '.join(ids)\n"
        "format_ids.py:2: TypeError"
    )
    assert _normalize_volatile_content(failure) == failure


# ── derive_key integration ──────────────────────────────────────────────────


def _trace(t: str, epoch: int, runner: str, sha: str, commit: str) -> str:
    """Mini synthetic trace with the same shape GitLab emits."""
    return (
        f"{t} 00O Running with gitlab-runner 19.0.0\n"
        f"{t} 00O   system ID: s_{sha[:12]}\n"
        f"{t} 00O section_start:{epoch}:prepare_executor\n"
        f"{t} 00O Using docker image sha256:{sha} for python:3.11-slim\n"
        f"{t} 01O Running on {runner} via minus\n"
        f"{t} 01O Gitaly correlation ID: 01KSR9QZV42TYJC8WKGA4EYHJV\n"
        f"{t} 01O Checking out {commit} as detached HEAD (ref is main)\n"
        f"{t} 01O 1 failed, 2 passed in 0.02s\n"
        f"{t} 01O FAILED test_calc.py::test_add - AssertionError: 4 != 5\n"
    )


def _body(trace: str) -> dict:
    return {
        "model": "qwen3-coder-480b-a35b-instruct",
        "messages": [
            {"role": "system", "content": "You are a Python bug fix agent."},
            {"role": "user", "content": f"## CI failure info\n{trace}"},
        ],
        "tools": [{"type": "function", "function": {"name": "submit_fix",
                                                    "parameters": {"type": "object"}}}],
        "temperature": 0.0,
    }


def test_two_runs_differing_only_in_metadata_hash_same_when_flag_on():
    """The headline contract: identical fixture, two pipeline runs.
    Timestamps, runner ID, docker SHA, commit all differ.
    With normalize_content=True the keys MUST match — otherwise the
    cache is useless for stress-test replay."""
    run1 = _trace(
        "2026-05-28T22:02:12.634090Z", 1780005732,
        "runner-sy3kr6y1-project-2-concurrent-0",
        "a3ab0b966bc4e91546a033e22093cb840908979487a9fc0e6e38295747e49ac0",
        "978e40b2",
    )
    run2 = _trace(
        "2026-05-29T03:14:09.999999Z", 1780051999,
        "runner-aa9bxxxx-project-2-concurrent-0",
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
        "1eb7c9c5",
    )
    assert derive_key(_body(run1), normalize_content=True) \
        == derive_key(_body(run2), normalize_content=True)


def test_two_runs_default_off_still_drift():
    """Without the flag, the existing behaviour is preserved — two
    runs of the same fixture DO get different keys (this was the
    observed 0 % hit rate in the live stress test). Pin this so
    a default-on regression would fail the test."""
    run1 = _trace(
        "2026-05-28T22:02:12.634090Z", 1780005732,
        "runner-sy3kr6y1-project-2-concurrent-0",
        "a3ab0b966bc4e91546a033e22093cb840908979487a9fc0e6e38295747e49ac0",
        "978e40b2",
    )
    run2 = _trace(
        "2026-05-29T03:14:09.999999Z", 1780051999,
        "runner-aa9bxxxx-project-2-concurrent-0",
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
        "1eb7c9c5",
    )
    # Default behaviour: distinct keys (otherwise the live observation
    # of 0 % hit rate wouldn't have surfaced the problem at all).
    assert derive_key(_body(run1)) != derive_key(_body(run2))


def test_normalize_content_does_not_change_genuine_diffs():
    """Two fixtures with GENUINELY different test failures must still
    produce different keys, even with normalisation on. The
    normalisation must not be so aggressive that it strips real
    fixture-distinguishing content."""
    run_calc = _trace(
        "2026-05-28T22:02:12.634090Z", 1780005732,
        "runner-sy3kr6y1-project-2-concurrent-0",
        "a3ab0b966bc4e91546a033e22093cb840908979487a9fc0e6e38295747e49ac0",
        "978e40b2",
    )
    # Different test failure body — different fixture.
    run_other = run_calc.replace(
        "FAILED test_calc.py::test_add - AssertionError: 4 != 5",
        "FAILED test_palindrome.py::test_mixed_case - AssertionError: False is True",
    )
    assert derive_key(_body(run_calc), normalize_content=True) \
        != derive_key(_body(run_other), normalize_content=True)


def test_assistant_messages_still_excluded_with_normalize():
    """The pre-existing "drop assistant messages" property must still
    hold when normalize_content is on. Otherwise the multi-turn ReAct
    flow regresses to per-turn key drift on the assistant turn."""
    body_a = _body(_trace(
        "2026-05-28T22:02:12.634090Z", 1780005732,
        "runner-sy3kr6y1-project-2-concurrent-0",
        "a3ab0b966bc4e91546a033e22093cb840908979487a9fc0e6e38295747e49ac0",
        "978e40b2",
    ))
    body_b = dict(body_a)
    body_b["messages"] = list(body_a["messages"]) + [{
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call_xyz", "type": "function",
                        "function": {"name": "fetch_additional_file",
                                     "arguments": '{"path": "calc.py"}'}}],
    }]
    assert derive_key(body_a, normalize_content=True) \
        == derive_key(body_b, normalize_content=True)
