from forms import validate


def test_valid_code():
    assert validate("ABC123") == "ok"


def test_valid_code_2():
    assert validate("xyz789") == "ok"


def test_too_short():
    assert validate("AB1") == "bad"


def test_letters_only():
    assert validate("ABCDEF") == "bad"
