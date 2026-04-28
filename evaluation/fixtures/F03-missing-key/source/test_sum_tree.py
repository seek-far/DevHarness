from sum_tree import sum_tree


def test_single_node_with_children_key():
    assert sum_tree({"val": 5, "children": []}) == 5


def test_leaf_without_children_key():
    assert sum_tree({"val": 7}) == 7


def test_nested():
    tree = {
        "val": 1,
        "children": [
            {"val": 2, "children": []},
            {"val": 3, "children": [{"val": 4, "children": []}]},
        ],
    }
    assert sum_tree(tree) == 10


def test_nested_with_some_leaves_missing_children():
    tree = {
        "val": 1,
        "children": [
            {"val": 2},
            {"val": 3, "children": [{"val": 4}]},
        ],
    }
    assert sum_tree(tree) == 10
