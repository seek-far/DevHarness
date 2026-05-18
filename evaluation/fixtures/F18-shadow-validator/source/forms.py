import re

from validators import is_valid_code


def _check(s):
    return bool(re.fullmatch(r"[A-Za-z]{3}\d{2}", s))


def validate(code):
    return "ok" if _check(code) else "bad"
