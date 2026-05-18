from producer import fetch


def first_present(keys):
    """Return the first key that exists in the store, else None."""
    for k in keys:
        if fetch(k):
            return k
    return None
