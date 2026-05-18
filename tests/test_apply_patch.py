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
