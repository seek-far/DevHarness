from clamp import clamp


def test_within():
    assert clamp(3, 10) == 3


def test_high():
    assert clamp(20, 10) == 10


def test_low():
    assert clamp(-20, 10) == -10


def test_neg_within():
    assert clamp(-5, 10) == -5


def test_high_2():
    assert clamp(15, 10) == 10
