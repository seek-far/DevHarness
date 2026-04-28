def sum_tree(node: dict) -> int:
    return node["val"] + sum(sum_tree(c) for c in node["children"])
