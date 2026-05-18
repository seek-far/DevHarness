from rounding import round_half_up


def test_pos_tie():
    assert round_half_up(0.5) == 1


def test_pos_tie_2():
    assert round_half_up(2.5) == 3


def test_neg_tie():
    assert round_half_up(-0.5) == -1


def test_neg_tie_2():
    assert round_half_up(-1.5) == -2


def test_non_tie():
    assert round_half_up(2.4) == 2
