from pages import pages


def test_exact_multiple():
    assert pages(9, 3) == 3


def test_one_over():
    assert pages(10, 3) == 4


def test_one_under():
    assert pages(8, 3) == 3


def test_zero_items():
    assert pages(0, 3) == 0


def test_single_item():
    assert pages(1, 3) == 1
