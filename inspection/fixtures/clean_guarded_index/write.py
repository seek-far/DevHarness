def set_line(lines, idx0, value):
    """Replace the 0-based line `idx0`. Raises IndexError if out of range."""
    if not (0 <= idx0 < len(lines)):
        raise IndexError(idx0)
    lines[idx0] = value
    return lines
