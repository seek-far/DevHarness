from first_n import first_n_chars


def test_basic():
    assert first_n_chars("hello", 3) == "hel"


def test_zero():
    assert first_n_chars("abc", 0) == ""


def test_n_exceeds_length():
    assert first_n_chars("abc", 10) == "abc"


def test_full_string():
    assert first_n_chars("abc", 3) == "abc"
