from clean import clean_whitespace


def test_already_clean():
    assert clean_whitespace("hello world") == "hello world"


def test_strips_ends():
    assert clean_whitespace("  hi  ") == "hi"


def test_collapses_long_run():
    assert clean_whitespace("a    b") == "a b"


def test_handles_tabs_and_newlines():
    assert clean_whitespace("a\t\tb\n\nc") == "a b c"
