import re


def is_valid_code(s):
    # canonical format: 3 letters followed by 3 digits, e.g. "ABC123"
    return bool(re.fullmatch(r"[A-Za-z]{3}\d{3}", s))
