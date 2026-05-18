from billing import monthly
from report import projected


def test_basic_month():
    assert monthly("basic") == 30


def test_plus_month():
    assert monthly("plus") == 15


def test_plus_projected():
    assert projected("plus", 4) == 60


def test_basic_projected():
    assert projected("basic", 2) == 60
