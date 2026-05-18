from ranges import span


def test_mid():
    assert span([1, 5, 10, 20], 4, 12) == 5


def test_neg():
    assert span([-3, -1, 2, 8], -2, 5) == 3


def test_too_few():
    assert span([1, 2, 3], 10, 20) == 0


def test_two_equal():
    assert span([4, 4], 1, 9) == 0
