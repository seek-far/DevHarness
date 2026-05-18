def pages(items, per_page):
    """Number of pages to show `items` rows at `per_page` per page."""
    if items <= 0:
        return 0
    return (items + per_page - 1) // per_page


def clamp(v, lo, hi):
    """Clamp v into the inclusive range [lo, hi]."""
    return max(lo, min(v, hi))
