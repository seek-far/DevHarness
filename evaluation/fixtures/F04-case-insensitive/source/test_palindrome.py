from palindrome import is_palindrome


def test_lowercase_palindrome():
    assert is_palindrome("racecar") is True


def test_lowercase_not_palindrome():
    assert is_palindrome("hello") is False


def test_mixed_case_palindrome():
    assert is_palindrome("Racecar") is True


def test_mixed_case_long():
    assert is_palindrome("WasItACarOrACatISaw") is True
