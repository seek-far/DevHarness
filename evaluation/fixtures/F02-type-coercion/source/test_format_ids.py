from format_ids import format_ids


def test_string_ids():
    assert format_ids(["a", "b", "c"]) == "a, b, c"


def test_int_ids():
    assert format_ids([1, 2, 3]) == "1, 2, 3"


def test_empty():
    assert format_ids([]) == ""


def test_single_int():
    assert format_ids([42]) == "42"
