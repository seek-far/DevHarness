from is_close_enough import is_close_enough


def test_exact_equal():
    assert is_close_enough(1.0, 1.0) is True


def test_classic_float_imprecision():
    # 0.1 + 0.2 != 0.3 exactly in IEEE 754, but they should be "close enough"
    assert is_close_enough(0.1 + 0.2, 0.3) is True


def test_clearly_unequal():
    assert is_close_enough(1.0, 2.0) is False


def test_small_difference_within_tolerance():
    assert is_close_enough(1.0, 1.0 + 1e-10) is True
