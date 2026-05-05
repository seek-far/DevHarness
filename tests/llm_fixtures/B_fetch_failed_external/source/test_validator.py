from validator import is_email


def test_email_requires_dot_after_at():
    assert is_email("user@example") is False


def test_email_accepts_full_address():
    assert is_email("user@example.com") is True
