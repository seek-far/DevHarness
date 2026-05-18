def parse_amount(text):
    """Parse a money string like "12.50" into integer cents."""
    try:
        return int(round(float(text) * 100))
    except Exception:
        return 0
