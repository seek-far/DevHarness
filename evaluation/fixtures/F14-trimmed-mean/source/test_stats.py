from stats import trimmed_mean


def test_skew():
    assert trimmed_mean([1, 2, 3, 100]) == 2.5


def test_neg():
    assert trimmed_mean([-10, 0, 5, 5]) == 2.5


def test_uniform():
    assert trimmed_mean([4, 4, 4, 4]) == 4.0


def test_five():
    assert trimmed_mean([1, 2, 3, 4, 5]) == 3.0
