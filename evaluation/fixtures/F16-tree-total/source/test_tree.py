from tree import total


def test_two_children():
    assert total((5, [(3, []), (2, [])])) == 10


def test_chain():
    assert total((1, [(2, [(3, [])])])) == 6


def test_single_leaf():
    assert total((7, [])) == 7
