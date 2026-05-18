def _collect(node, acc):
    acc.append(node[0])
    for child in node[1]:
        _collect(child, acc)
    return acc


def total(tree):
    vals = _collect(tree, [])
    return sum(vals[1:])
