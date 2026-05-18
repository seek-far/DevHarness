def timeout_for(opts):
    """Return the request timeout in seconds.

    'timeout' is an OPTIONAL key; when absent the documented default of 30s
    applies. Callers rely on this default.
    """
    return opts.get("timeout", 30)
