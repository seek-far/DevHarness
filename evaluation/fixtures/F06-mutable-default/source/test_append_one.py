from append_one import append_one


def test_first_call_returns_single_item_list():
    assert append_one(1) == [1]


def test_independent_calls_do_not_share_state():
    # If the default is a mutable shared list, the second call would see [1, 2].
    a = append_one("x")
    b = append_one("y")
    assert a == ["x"]
    assert b == ["y"]


def test_explicit_list_appends():
    assert append_one(99, [1, 2]) == [1, 2, 99]
