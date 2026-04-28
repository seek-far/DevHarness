from average import average


def test_basic():
    assert average([1, 2, 3]) == 2.0


def test_empty_returns_zero():
    assert average([]) == 0.0


def test_single_element():
    assert average([5]) == 5.0


def test_negative_and_positive():
    assert average([-2, 2]) == 0.0
