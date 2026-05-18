from pathlib import Path


class PatchAnchorError(Exception):
    pass


def apply_change(src_filepath, line_number, original_line, new_line):
    """Replace one line, anchored on content. line_number is only a hint."""
    p = Path(src_filepath)
    lines = p.read_text().split("\n")
    idx = line_number - 1
    if 0 <= idx < len(lines) and lines[idx] == original_line:
        target = idx
    else:
        matches = [i for i, s in enumerate(lines) if s == original_line]
        if len(matches) != 1:
            raise PatchAnchorError(
                f"cannot anchor {original_line!r}: {len(matches)} match(es)"
            )
        target = matches[0]
    lines[target] = new_line
    p.write_text("\n".join(lines), encoding="utf-8")
