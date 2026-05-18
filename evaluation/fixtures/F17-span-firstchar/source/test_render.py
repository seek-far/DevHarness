from render import render


def test_two_words():
    assert render("hi there") == "[hi] [there]"


def test_single_word():
    assert render("alpha") == "[alpha]"


def test_varied():
    assert render("a bb ccc") == "[a] [bb] [ccc]"
