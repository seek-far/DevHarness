"""
apply_patch — apply LLM line edits to a source file.

Contract (this is what `submit_fix` entries must satisfy):
  Each change is {line_number, original_line, new_line}. The patch is
  ANCHORED ON CONTENT: `original_line` must be the verbatim current text of
  the line being replaced. `line_number` is only a 1-based *hint*.

Why anchored (not blind index assignment): the previous implementation did
`src_lines[line_number-1] = new_line` with no check on `original_line`. On
multi-edit / retry the model's line numbers go stale (a prior attempt already
rewrote lines), so a blind index write *silently corrupts* an
already-correct file — observed repeatedly (F11/F14/F17: correct reasoning,
destroyed by a stale-index write, surfacing only as a later NameError).

Resolution per change:
  1. If the hinted line already equals `original_line` → apply there.
  2. Else locate `original_line` by content:
       - exactly one match → apply there (self-heal a stale line_number;
         this rescues the dominant real failure mode).
       - it occurs multiple times → use the hint if it points at one of
         them, otherwise reject as ambiguous.
       - not found (after a whitespace-tolerant retry) → reject.
  3. Empty `original_line` is rejected: every change must anchor on a real
     existing line (no unanchored writes — that is what caused the silent
     corruption).

Insertion: `new_line` MAY contain multiple lines (split on "\n"). The
anchored line is spliced out and replaced by those lines, so the model adds
code by replacing one real line with `that line + the additions`, all as
natural multi-line Python — no `;`-crammed statements, no unanchored insert.

A rejection raises `PatchAnchorError`; `apply_change_and_test` turns that
into the existing `apply_error → retry` feedback (so the LLM sees a precise,
actionable message and the reflection apply-crash branch fires) instead of a
silent file corruption.
"""

from pathlib import Path
import logging

logger = logging.getLogger("bf_agent")


class PatchAnchorError(Exception):
    """A change could not be anchored to a verbatim current line."""


def _resolve_index(src_lines: list[str], change: dict, src_filepath: str) -> int:
    if not isinstance(change, dict):
        raise PatchAnchorError(
            f"{src_filepath}: each fix must be an object with "
            f"line_number/original_line/new_line, got {type(change).__name__}: "
            f"{change!r:.80}"
        )
    ln = change.get("line_number")
    original = change.get("original_line")
    if original is None or original == "":
        raise PatchAnchorError(
            f"{src_filepath}: fix is missing `original_line` (got {original!r}). "
            f"This applier REPLACES one existing line; it cannot insert. Set "
            f"`original_line` to the verbatim current text of the line to change."
        )

    n = len(src_lines)
    hint = (ln - 1) if isinstance(ln, int) else None

    # 1. Hint is exact — fast path.
    if hint is not None and 0 <= hint < n and src_lines[hint] == original:
        return hint

    # 2. Anchor by exact content.
    exact = [i for i, s in enumerate(src_lines) if s == original]
    if len(exact) == 1:
        if exact[0] != hint:
            logger.warning(
                "apply_patch: stale line_number for %s (hint=%s, found content "
                "at line %d) — self-healing", src_filepath, ln, exact[0] + 1,
            )
        return exact[0]
    if len(exact) > 1:
        if hint in exact:
            return hint
        raise PatchAnchorError(
            f"{src_filepath}: `original_line` {original!r} occurs "
            f"{len(exact)} times and line_number={ln} points at none of them. "
            f"Give a line_number that matches one of the occurrences."
        )

    # 3. Whitespace-tolerant retry (model often gets indentation slightly off).
    key = original.strip()
    if key:
        loose = [i for i, s in enumerate(src_lines) if s.strip() == key]
        if len(loose) == 1:
            logger.warning(
                "apply_patch: %s matched line %d only after trimming whitespace",
                src_filepath, loose[0] + 1,
            )
            return loose[0]

    at_hint = (
        repr(src_lines[hint]) if hint is not None and 0 <= hint < n else "<out of range>"
    )
    raise PatchAnchorError(
        f"{src_filepath}: could not find `original_line` {original!r} in the "
        f"current file (line_number={ln} currently holds {at_hint}). The file "
        f"may have been changed by a previous attempt — set `original_line` to "
        f"the verbatim CURRENT text of the line you want to replace."
    )


def apply_change_infos(src_filepath: str, change_infos: list[dict]):
    src_filepath = Path(src_filepath)
    src_lines = src_filepath.read_text().split('\n')
    # Pure replacement keeps indices stable, so resolving each change against
    # the running buffer is equivalent to resolving against the original and
    # also lets a later edit see an earlier edit's result.
    for change_info in change_infos:
        idx = _resolve_index(src_lines, change_info, str(src_filepath))
        # new_line may be multi-line: splice replaces the one anchored line
        # with 1+ lines. Pure replacement (1 line) keeps len stable; an
        # insert grows it. Later changes are resolved by content against the
        # mutated buffer, so a shifted line_number is harmless.
        src_lines[idx:idx + 1] = str(change_info['new_line']).split("\n")
    src_filepath.write_text("\n".join(src_lines), encoding="utf-8")


if __name__ == '__main__':
    import platform
    src_filepath = '/my_git/restaurant_order_demo__order_be_bf/api/views.py'
    if platform.system() == "Linux":
        src_filepath = "/mnt/d" + src_filepath
    change_infos = [
        {
            "line_number": 6,
            "original_line": "    queryset = Dish.all().order_by('id')",
            "new_line": "    queryset = Dish.objects.all().order_by('id')",
        }
    ]
    apply_change_infos(src_filepath=src_filepath, change_infos=change_infos)
