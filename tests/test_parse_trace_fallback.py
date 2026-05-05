"""Tests for the parse_trace fallback path.

Behavior under test:
  1. Empty / whitespace-only trace still raises (the raw-trace fallback is for
     unparseable, not empty).
  2. When the parser cannot extract a structured error/path, the node falls
     back to raw-trace mode: error_info=raw trace tail, suspect_file_path="",
     parse_trace_fallback=True. No exception.
  3. The raw trace forwarded to error_info is tail-truncated when oversized
     (pytest prints the actionable failure at the bottom).
  4. The successful-parse path still sets parse_trace_fallback=False, so
     downstream consumers can branch unambiguously.
  5. The graph routing function sends fallback state to react_loop and parsed
     state to fetch_source_file.
  6. _build_initial_messages branches on parse_trace_fallback: in fallback
     mode the "Suspect file" section is replaced by a "no suspect file"
     instruction and no source_block is emitted.
  7. The system prompt requires every fix entry to set file_path explicitly
     when no suspect file was pre-identified.
  8. apply_change_and_test rejects fix entries with no file_path (and no
     suspect file) via apply_error → retry channel.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from graph.nodes.parse_trace import _RAW_TRACE_TAIL, parse_trace  # noqa: E402
from graph.nodes.react_loop import _SYSTEM, _build_initial_messages  # noqa: E402
from graph.routing import route_after_parse_trace  # noqa: E402


# ── parse_trace node ─────────────────────────────────────────────────────────


def test_empty_trace_raises():
    with pytest.raises(ValueError, match="empty"):
        parse_trace({"trace": ""})


def test_whitespace_only_trace_raises():
    with pytest.raises(ValueError, match="empty"):
        parse_trace({"trace": "   \n\t\n  "})


def test_parser_failure_falls_back_to_raw_trace():
    trace = "some random log lines\nwithout any pytest E-line or .py:N reference\n"
    out = parse_trace({"trace": trace})
    assert out["parse_trace_fallback"] is True
    assert out["suspect_file_path"] == ""
    # Whole trace fits under the cap, so it's forwarded verbatim.
    assert out["error_info"] == trace


def test_fallback_truncates_oversized_trace_to_tail():
    # Build a trace larger than _RAW_TRACE_TAIL with a unique tail marker so
    # we can confirm the tail (not the head) is preserved. The marker itself
    # must not contain pytest-style "E " lines or "<path>.py:<n>" patterns,
    # otherwise the parser would succeed and we'd skip the fallback.
    head = "x" * (_RAW_TRACE_TAIL * 2)
    tail = "ZZZ_TAIL_MARKER_END"
    trace = head + "\n" + tail
    out = parse_trace({"trace": trace})
    assert out["parse_trace_fallback"] is True
    assert out["error_info"].endswith(tail)
    assert out["error_info"].startswith("...[head truncated]")
    # Forwarded length is _RAW_TRACE_TAIL plus the truncation marker prefix.
    assert len(out["error_info"]) <= _RAW_TRACE_TAIL + 32


def test_successful_parse_sets_fallback_false():
    trace = (
        "tests/test_x.py::test_y FAILED\n"
        "E   AssertionError: expected 2, got 1\n"
        "src/calc.py:42: AssertionError\n"
    )
    out = parse_trace({"trace": trace})
    assert out["parse_trace_fallback"] is False
    assert out["suspect_file_path"] == "src/calc.py"
    assert "AssertionError" in out["error_info"]


# ── routing ──────────────────────────────────────────────────────────────────


def test_route_after_parse_trace_normal_path():
    state = {"suspect_file_path": "src/calc.py"}
    assert route_after_parse_trace(state) == "fetch_source_file"


def test_route_after_parse_trace_fallback_empty_path():
    state = {"suspect_file_path": "", "parse_trace_fallback": True}
    assert route_after_parse_trace(state) == "react_loop"


def test_route_after_parse_trace_missing_path_key():
    # A defensive fallback: if the key is somehow missing, we still route to
    # react_loop rather than crashing fetch_source_file with a KeyError.
    assert route_after_parse_trace({}) == "react_loop"


# ── react_loop prompt branching ──────────────────────────────────────────────


def test_system_prompt_requires_file_path_in_fallback_mode():
    # The system prompt must tell the LLM that file_path is REQUIRED on every
    # fix when no suspect file is pre-identified — otherwise apply_patch's
    # fallback target would be empty and writes would corrupt apply.
    assert "No suspect file pre-identified" in _SYSTEM
    assert "file_path" in _SYSTEM
    # The "REQUIRED" wording is what makes the rule unambiguous to the model.
    assert "EXPLICITLY" in _SYSTEM or "explicitly" in _SYSTEM


def test_build_initial_messages_fallback_branch_omits_source_block():
    state = {
        "trace": "garbage",
        "error_info": "garbage trace contents — no parse possible",
        "suspect_file_path": "",
        "parse_trace_fallback": True,
        # No source_file_content key on purpose: fetch_source_file was skipped.
        "fix_retry_count": 0,
    }
    msgs = _build_initial_messages(state)
    [human] = [m for m in msgs if m.__class__.__name__ == "HumanMessage"]
    text = human.content
    assert "No suspect file pre-identified" in text
    assert "fetch_additional_file" in text
    # Must NOT echo a source block — there is no source content to show.
    assert "```python" not in text
    # Must NOT claim a suspect file when there isn't one.
    assert "## Suspect file:" not in text


def test_build_initial_messages_normal_path_unchanged():
    # Regression guard for the parsed-path: existing prompt sections must
    # still appear when parse_trace_fallback is False.
    state = {
        "error_info": "AssertionError",
        "source_file_content": "def add(a, b): return a - b\n",
        "suspect_file_path": "calc.py",
        "parse_trace_fallback": False,
        "fix_retry_count": 0,
    }
    msgs = _build_initial_messages(state)
    [human] = [m for m in msgs if m.__class__.__name__ == "HumanMessage"]
    text = human.content
    assert "## Suspect file: calc.py" in text
    assert "```python" in text
    assert "No suspect file pre-identified" not in text


# ── apply_change_and_test rejection ──────────────────────────────────────────


def test_apply_rejects_missing_file_path_when_no_suspect():
    # We exercise only the up-front grouping logic — no provider/venv work
    # happens before the rejection. Use a stub provider whose ensure_repo_ready
    # returns a path; the rejection short-circuits before any disk write.
    from graph.nodes.apply_change_and_test import apply_change_and_test

    class _StubProvider:
        def ensure_repo_ready(self, bug_id):
            return Path("/tmp/does_not_matter_rejected_first")

    state = {
        "provider": _StubProvider(),
        "bug_id": "BUG-1",
        "suspect_file_path": "",                    # fallback mode
        "llm_result": {
            "fixes": [
                # Missing file_path AND no suspect → must be rejected.
                {"original": "a", "replacement": "b"},
            ],
        },
        "fix_retry_count": 0,
    }
    out = apply_change_and_test(state)
    assert out["test_passed"] is False
    assert "missing required `file_path`" in out["apply_error"]
    assert out["fix_retry_count"] == 1
    # test_output must echo the rejection so the retry feedback channel can
    # surface it back to the LLM (sanitized) on the next react_loop turn.
    assert "apply rejected" in out["test_output"]
