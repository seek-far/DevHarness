RATES = {
    "basic": 1.0,
    "plus": 0.5,
    "pro": 3.0,
}


def rate_for(plan):
    """Return the multiplier for `plan`, or 1.0 if the plan is unknown."""
    return RATES.get(plan, 1.0)
