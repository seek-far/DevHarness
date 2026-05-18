def is_between(x, lo, hi):
    return lo <= x <= hi


def select(values, lo, hi):
    return [v for v in values if is_between(v, lo, hi)]


def span(values, lo, hi):
    sel = select(values, lo, hi)
    if len(sel) < 2:
        return 0
    return max(sel) - min(values)
