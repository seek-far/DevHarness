"""Unit tests for the content-anchored patch applier.

The old applier did blind `src_lines[line_number-1] = new_line`, which
silently corrupted an already-correct file when the model's line_number went
stale on a retry (observed: F11/F14/F17 — correct reasoning destroyed by a
stale-index write). The applier now anchors on `original_line`: it self-heals
a stale line_number when the content is uniquely locatable, and rejects
(PatchAnchorError) instead of corrupting when it cannot.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from services.apply_patch import PatchAnchorError, apply_change_infos  # noqa: E402


def _write(tmp_path: Path, text: str) -> str:
    p = tmp_path / "m.py"
    p.write_text(text)
    return str(p)


def test_exact_hint_applies(tmp_path):
    f = _write(tmp_path, "a = 1\nb = 2\nc = 3")
    apply_change_infos(f, [{"line_number": 2, "original_line": "b = 2",
                            "new_line": "b = 20"}])
    assert Path(f).read_text() == "a = 1\nb = 20\nc = 3"


def test_self_heals_stale_line_number(tmp_path):
    # line_number points at the wrong line, but original_line content is
    # unique → apply at the real location instead of corrupting line 1.
    f = _write(tmp_path, "a = 1\nb = 2\nc = 3")
    apply_change_infos(f, [{"line_number": 1, "original_line": "c = 3",
                            "new_line": "c = 30"}])
    assert Path(f).read_text() == "a = 1\nb = 2\nc = 30"


def test_stale_anchor_rejected_not_corrupting(tmp_path):
    # The F11/F14/F17 signature: a prior attempt already rewrote the line, so
    # original_line no longer exists. Must reject, NOT blind-write line 2.
    f = _write(tmp_path, "def f():\n    return fixed()\n")
    with pytest.raises(PatchAnchorError):
        apply_change_infos(f, [{"line_number": 2,
                                "original_line": "    return old()",
                                "new_line": "    return other()"}])
    assert Path(f).read_text() == "def f():\n    return fixed()\n"  # untouched


def test_empty_original_line_rejected(tmp_path):
    f = _write(tmp_path, "x = 1\n")
    with pytest.raises(PatchAnchorError):
        apply_change_infos(f, [{"line_number": 1, "original_line": "",
                                "new_line": "import math"}])


def test_ambiguous_content_uses_hint_or_rejects(tmp_path):
    f = _write(tmp_path, "    pass\nif a:\n    pass\nif b:\n    pass")
    # hint points at one of the duplicates → use it
    apply_change_infos(f, [{"line_number": 3, "original_line": "    pass",
                            "new_line": "    return 1"}])
    assert Path(f).read_text().split("\n")[2] == "    return 1"
    # hint points at none of the duplicates → ambiguous reject
    with pytest.raises(PatchAnchorError):
        apply_change_infos(f, [{"line_number": 99, "original_line": "    pass",
                                "new_line": "    return 2"}])


def test_whitespace_tolerant_match(tmp_path):
    f = _write(tmp_path, "def f():\n        return 1\n")  # 8-space indent
    apply_change_infos(f, [{"line_number": 2, "original_line": "    return 1",
                            "new_line": "        return 2"}])
    assert Path(f).read_text() == "def f():\n        return 2\n"


def test_multiline_new_line_inserts(tmp_path):
    # F06-style: add a guard by replacing the anchored line with itself + new
    # lines (real newlines). No empty original_line, no ';' cramming.
    f = _write(tmp_path, "def append_one(value, items=None):\n    items.append(value)\n    return items")
    apply_change_infos(f, [{
        "line_number": 2,
        "original_line": "    items.append(value)",
        "new_line": "    if items is None:\n        items = []\n    items.append(value)",
    }])
    assert Path(f).read_text() == (
        "def append_one(value, items=None):\n"
        "    if items is None:\n"
        "        items = []\n"
        "    items.append(value)\n"
        "    return items"
    )


def test_non_dict_fix_entry_rejected(tmp_path):
    # The 1/50 crash: a malformed (string) fix entry must reject cleanly,
    # not raise AttributeError ('str' object has no attribute 'get').
    f = _write(tmp_path, "x = 1\n")
    with pytest.raises(PatchAnchorError):
        apply_change_infos(f, ["not a dict"])


def test_multi_edit_after_insert_still_anchors(tmp_path):
    # After a multi-line splice shifts indices, a later change must still
    # resolve by content, not stale line_number.
    f = _write(tmp_path, "a = 1\nb = 2\nc = 3")
    apply_change_infos(f, [
        {"line_number": 1, "original_line": "a = 1",
         "new_line": "import os\na = 1"},                 # +1 line
        {"line_number": 3, "original_line": "c = 3",       # stale hint (now 4)
         "new_line": "c = 30"},
    ])
    assert Path(f).read_text() == "import os\na = 1\nb = 2\nc = 30"


def test_multi_edit_each_anchored(tmp_path):
    f = _write(tmp_path, "a = 1\nb = 2\nc = 3")
    apply_change_infos(f, [
        {"line_number": 1, "original_line": "a = 1", "new_line": "a = 10"},
        {"line_number": 3, "original_line": "c = 3", "new_line": "c = 30"},
    ])
    assert Path(f).read_text() == "a = 10\nb = 2\nc = 30"


# ── whitespace-tolerant rebase: regression tests for F17-span-firstchar ──────
# Qwen2.5-Coder-Instruct on self-hosted vLLM dropped the leading indentation
# from both `original_line` and `new_line`. The whitespace-tolerant anchor
# still found the right line (good), but writing `new_line` verbatim put the
# replacement at column 0 → IndentationError → pytest collection failure →
# spurious "fix broke the build" outcome. The anchored applier must rebase
# the LLM's indentation onto the on-disk line's leading whitespace.


def test_rebase_when_llm_dropped_indent_single_line(tmp_path):
    """The F17 shape: LLM submits `start = i` with no indent; on-disk has
    8 spaces of indent. Rebase prepends the on-disk indent so the
    replacement stays inside its enclosing block."""
    f = _write(tmp_path, "def f():\n    for i in range(3):\n        start = 1\n")
    apply_change_infos(f, [{
        "line_number": 3,
        "original_line": "start = 1",   # LLM dropped the indent
        "new_line": "start = i",        # …and submitted new_line at column 0
    }])
    assert Path(f).read_text() == (
        "def f():\n    for i in range(3):\n        start = i\n"
    )


def test_rebase_when_llm_uses_source_indent_framing(tmp_path):
    """Variant of F17: LLM has a 4-space mental model of the line, but the
    on-disk line is at 8 spaces. Both `original_line` and `new_line` were
    written at the LLM's 4-space framing. The rebase swaps the prefix."""
    f = _write(tmp_path, "def f():\n        x = 1\n")
    apply_change_infos(f, [{
        "line_number": 2,
        "original_line": "    x = 1",   # LLM's framing: 4 spaces
        "new_line": "    x = 2",        # same framing
    }])
    assert Path(f).read_text() == "def f():\n        x = 2\n"


def test_rebase_no_op_when_llm_compensated(tmp_path):
    """The pre-existing test_whitespace_tolerant_match scenario: LLM
    submits new_line ALREADY at the on-disk indent (looking at the file as
    they write the replacement). Rebase must leave it alone — no
    double-indentation. (Mirrors the existing test, kept explicitly under
    the new contract.)"""
    f = _write(tmp_path, "def f():\n        return 1\n")
    apply_change_infos(f, [{
        "line_number": 2,
        "original_line": "    return 1",     # 4 spaces in LLM's framing
        "new_line": "        return 2",      # already 8 — on-disk indent
    }])
    assert Path(f).read_text() == "def f():\n        return 2\n"


def test_rebase_preserves_relative_indent_in_multiline_new_line(tmp_path):
    """Multi-line `new_line` where outer line uses source's framing and
    deeper lines use a relative offset from it. Each line is rebased
    against the same on-disk indent so the relative offsets are preserved."""
    f = _write(tmp_path, "def f():\n        do_a()\n")
    apply_change_infos(f, [{
        "line_number": 2,
        "original_line": "do_a()",                    # LLM dropped indent
        "new_line": "if cond:\n    do_a()\nelse:\n    do_b()",
    }])
    # First line at 8 spaces (on-disk), inner lines at on-disk + their own
    # 4-space relative indent (so the on-disk line goes from `        do_a()`
    # to a 4-line block whose outer level is 8 and inner is 12).
    assert Path(f).read_text() == (
        "def f():\n"
        "        if cond:\n"
        "            do_a()\n"
        "        else:\n"
        "            do_b()\n"
    )


def test_rebase_leaves_exact_match_alone(tmp_path):
    """When the LLM nails the indent exactly (the common, non-rebase path),
    the rebase function is a no-op — file is unchanged except for the
    replacement bytes."""
    f = _write(tmp_path, "def f():\n        x = 1\n")
    apply_change_infos(f, [{
        "line_number": 2,
        "original_line": "        x = 1",
        "new_line": "        x = 2",
    }])
    assert Path(f).read_text() == "def f():\n        x = 2\n"


def test_rebase_handles_blank_lines_inside_multiline_new_line(tmp_path):
    """Blank lines inside a multi-line `new_line` must NOT have whitespace
    prepended — they should stay empty so the block remains a real Python
    blank line, not a column-8 trailing-whitespace line."""
    f = _write(tmp_path, "def f():\n        x = 1\n")
    apply_change_infos(f, [{
        "line_number": 2,
        "original_line": "x = 1",
        "new_line": "x = 1\n\ny = 2",
    }])
    text = Path(f).read_text()
    lines = text.split("\n")
    # Find the blank line and check it has no leading whitespace.
    [blank_idx] = [i for i, ln in enumerate(lines) if ln == ""][:1]
    assert lines[blank_idx] == ""
