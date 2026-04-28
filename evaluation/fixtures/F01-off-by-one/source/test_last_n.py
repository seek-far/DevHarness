from last_n import get_last_n


def test_basic():
    assert get_last_n([1, 2, 3, 4, 5], 2) == [4, 5]


def test_zero_returns_empty():
    assert get_last_n([1, 2, 3], 0) == []


def test_more_than_len_returns_all():
    assert get_last_n([1, 2, 3], 10) == [1, 2, 3]


def test_empty_input():
    assert get_last_n([], 3) == []
