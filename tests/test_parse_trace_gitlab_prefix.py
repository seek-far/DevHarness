"""Tests for the GitLab-prefix fix in `services.parse_trace`.

Background: GitLab CI runner prefixes every captured pytest line with
a timestamp + stream marker:

    2026-05-28T22:02:21.669535Z 01O E       TypeError: ...

The pre-fix `parse_trace` used `re.match(r"E\\s+...")` anchored at line
start, so the position-0 `2` (from the year) defeated the match on
EVERY GitLab-collected trace. The result: 100 % of fixtures fell into
the "No suspect file pre-identified" fallback, each fixture burned
1-2 extra `fetch_additional_file` LLM calls (verified live 2026-05-29:
404 fallback prompts vs 224 unparseable `01O E\\s+\\w+Error:` lines).

These tests pin:
  * Each of the 4 common pytest error shapes (AssertionError,
    TypeError, ZeroDivisionError, KeyError) extracts both
    `error_message` and `suspect_files` when wrapped in GitLab's
    timestamp+stream prefix.
  * The raw (un-prefixed) pytest output STILL parses identically
    (regression coverage for the legacy `integration_test.py` path
    and any future caller that feeds raw pytest output).
  * ANSI escape codes embedded in the line are stripped before regex
    matching.
  * An unparseable trace still returns None/None gracefully.
  * The PREFIX itself is preserved on the returned `error_message`
    (only the matcher sees the stripped form) so downstream prompt
    rendering reproduces the operator-visible trace verbatim.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from services.parse_trace import _strip_trace_prefix, parse_trace  # noqa: E402


# ── _strip_trace_prefix ─────────────────────────────────────────────────────


def test_strip_gitlab_timestamp_and_stream_marker():
    line = "2026-05-28T22:02:21.669535Z 01O E       TypeError: foo"
    assert _strip_trace_prefix(line) == "E       TypeError: foo"


def test_strip_handles_stderr_stream():
    line = "2026-05-28T22:02:21.787558Z 01E WARNING: pip warning"
    assert _strip_trace_prefix(line) == "WARNING: pip warning"


def test_strip_handles_continuation_marker():
    """GitLab uses `00O+` for continuation lines (no newline emitted by
    the underlying process). Must still strip clean."""
    line = "2026-05-28T22:02:16.394764Z 00O+section_start:1780005736:prepare_script"
    assert _strip_trace_prefix(line) == "section_start:1780005736:prepare_script"


def test_strip_handles_ansi_color_codes():
    line = "2026-05-28T22:02:21.589663Z 01O \x1b[32;1m$ pip install\x1b[0m"
    assert _strip_trace_prefix(line) == "$ pip install"


def test_strip_idempotent_on_unwrapped_line():
    """Raw pytest output (no prefix) must pass through unchanged so
    the legacy integration_test.py code path keeps parsing
    byte-identically."""
    line = "E       TypeError: sequence item 0: expected str instance, int found"
    assert _strip_trace_prefix(line) == line


def test_strip_handles_no_microseconds():
    """ISO 8601 timestamp without fractional seconds — some GitLab
    configurations omit microseconds."""
    line = "2026-05-28T22:02:21Z 01O E   AssertionError"
    assert _strip_trace_prefix(line) == "E   AssertionError"


# ── parse_trace on GitLab-wrapped traces, four error types ──────────────────


def _gitlab_trace(snippet: str, ts_start: str = "2026-05-28T22:02:21") -> str:
    """Wrap a pytest snippet with GitLab's per-line timestamp prefix.
    Each non-empty source line gets a fresh microsecond-incremented
    timestamp so the test data looks like the real thing."""
    out = []
    micro = 0
    for line in snippet.splitlines():
        if line.strip():
            out.append(f"{ts_start}.{micro:06d}Z 01O {line}")
        else:
            out.append(f"{ts_start}.{micro:06d}Z 01O ")
        micro += 100
    return "\n".join(out)


def test_gitlab_wrapped_typeerror_parses():
    """The exact shape from fixture F02 (format_ids) that surfaced the
    bug. The pre-fix code returned None/None for this trace."""
    snippet = (
        "FAILED test_format_ids.py::test_int_ids - TypeError: sequence item 0: expected str instance, int found\n"
        "    def format_ids(ids) -> str:\n"
        ">       return ', '.join(ids)\n"
        "                ^^^^^^^^^^^^^^\n"
        "E       TypeError: sequence item 0: expected str instance, int found\n"
        "format_ids.py:2: TypeError"
    )
    result = parse_trace(_gitlab_trace(snippet))
    assert result["error_message"] is not None
    assert "TypeError" in result["error_message"]
    assert result["suspect_files"] == [
        {"file_path": "format_ids.py", "line_number_1_based": 2}
    ]


def test_gitlab_wrapped_assertionerror_parses():
    """The fixture F04 (palindrome) shape — pure AssertionError."""
    snippet = (
        "FAILED test_palindrome.py::test_mixed_case - AssertionError: assert False is True\n"
        "    def test_mixed_case_palindrome():\n"
        ">       assert is_palindrome('Racecar') is True\n"
        "E       AssertionError: assert False is True\n"
        "E        +  where False = is_palindrome('Racecar')\n"
        "test_palindrome.py:13: AssertionError"
    )
    result = parse_trace(_gitlab_trace(snippet))
    assert result["error_message"] is not None
    assert "AssertionError" in result["error_message"]
    assert result["suspect_files"] == [
        {"file_path": "test_palindrome.py", "line_number_1_based": 13}
    ]


def test_gitlab_wrapped_zerodivision_parses():
    snippet = (
        "FAILED test_average.py::test_empty_returns_zero - ZeroDivisionError: division by zero\n"
        "    def average(nums: list) -> float:\n"
        ">       return sum(nums) / len(nums)\n"
        "E       ZeroDivisionError: division by zero\n"
        "average.py:2: ZeroDivisionError"
    )
    result = parse_trace(_gitlab_trace(snippet))
    assert result["error_message"] is not None
    assert "ZeroDivisionError" in result["error_message"]
    assert result["suspect_files"] == [
        {"file_path": "average.py", "line_number_1_based": 2}
    ]


def test_gitlab_wrapped_keyerror_parses():
    """KeyError shows the missing key in quotes — make sure that
    doesn't confuse the `.+` capture."""
    snippet = (
        "FAILED test_sum_tree.py - KeyError: 'children'\n"
        "    def sum_tree(node):\n"
        ">       return node['value'] + sum(sum_tree(c) for c in node['children'])\n"
        "E       KeyError: 'children'\n"
        "sum_tree.py:2: KeyError"
    )
    result = parse_trace(_gitlab_trace(snippet))
    assert result["error_message"] is not None
    assert "KeyError" in result["error_message"]
    assert result["suspect_files"] == [
        {"file_path": "sum_tree.py", "line_number_1_based": 2}
    ]


def test_gitlab_wrapped_pure_assert_no_class_parses():
    """When pytest's `assert` introspection prints just `E   assert ...`
    without naming AssertionError, the second regex branch kicks in."""
    snippet = (
        "def test_thing():\n"
        ">   assert format_ids([1, 2, 3]) == '1, 2, 3'\n"
        "E   assert None == '1, 2, 3'\n"
        "E    +  where None = format_ids([1, 2, 3])\n"
        "format_ids.py:2: AssertionError"
    )
    result = parse_trace(_gitlab_trace(snippet))
    assert result["error_message"] is not None
    assert "assert" in result["error_message"]
    assert result["suspect_files"] == [
        {"file_path": "format_ids.py", "line_number_1_based": 2}
    ]


# ── ANSI-coloured prefix path ───────────────────────────────────────────────


def test_gitlab_wrapped_with_ansi_colour_parses():
    """The real-world traces have ANSI codes inside the prefix
    (`\\x1b[32;1m...`). Must strip both prefix AND ANSI."""
    raw_lines = [
        "2026-05-28T22:02:21.589663Z 01O \x1b[32;1m$ pytest -q\x1b[0m",
        "2026-05-28T22:02:21.669527Z 01O =================================== FAILURES ===================================",
        "2026-05-28T22:02:21.669535Z 01O E       TypeError: nope",
        "2026-05-28T22:02:21.669568Z 01O format_ids.py:2: TypeError",
    ]
    result = parse_trace("\n".join(raw_lines))
    assert result["error_message"] is not None
    assert "TypeError" in result["error_message"]
    assert result["suspect_files"] == [
        {"file_path": "format_ids.py", "line_number_1_based": 2}
    ]


# ── regression: raw (un-wrapped) pytest still works ─────────────────────────


def test_raw_pytest_output_still_parses_byte_identically():
    """The legacy `integration_test.py` path feeds raw pytest output
    (no GitLab prefix). The fix must not break it."""
    raw = (
        "    def test_int_ids():\n"
        ">       assert format_ids([1, 2, 3]) == '1, 2, 3'\n"
        "E       TypeError: sequence item 0: expected str instance, int found\n"
        "format_ids.py:2: TypeError"
    )
    result = parse_trace(raw)
    assert "TypeError" in result["error_message"]
    assert result["suspect_files"] == [
        {"file_path": "format_ids.py", "line_number_1_based": 2}
    ]


# ── unparseable trace still graceful ────────────────────────────────────────


def test_unparseable_gitlab_trace_returns_none():
    """A trace that genuinely has no `E   ErrorClass:` or `E   assert`
    line (e.g. just runner machinery) must still return None/None
    instead of crashing."""
    snippet = (
        "Running with gitlab-runner 19.0.0\n"
        "Preparing the docker executor\n"
        "Job succeeded somehow"
    )
    result = parse_trace(_gitlab_trace(snippet))
    assert result == {"error_message": None, "suspect_files": None}


# ── error_message keeps the prefix (preserves operator-visible context) ─────


def test_error_message_preserves_original_prefix():
    """The matcher operates on the stripped form, but the returned
    `error_message` keeps the original lines so the LLM prompt
    reproduces exactly what an operator would see in GitLab."""
    snippet = (
        "FAILED test_x.py::test_y - TypeError: nope\n"
        "E       TypeError: nope\n"
        "x.py:1: TypeError"
    )
    result = parse_trace(_gitlab_trace(snippet))
    # Original timestamps still in the surfaced message.
    assert "2026-05-28T22:02:21" in result["error_message"]
    assert "01O" in result["error_message"]
