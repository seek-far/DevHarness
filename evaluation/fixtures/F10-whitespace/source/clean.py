def clean_whitespace(s: str) -> str:
    """Collapse internal whitespace runs into single spaces and strip ends."""
    return s.replace("  ", " ").strip()
