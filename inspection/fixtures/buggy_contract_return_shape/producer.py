_DB = {"a": 1, "b": 2}


def fetch(key):
    """Return (found: bool, value). value is None when not found."""
    if key in _DB:
        return True, _DB[key]
    return False, None
