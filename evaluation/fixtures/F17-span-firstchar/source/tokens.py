def spans(text):
    out = []
    i = 0
    for k, word in enumerate(text.split(" ")):
        start = i if k > 0 else 1
        end = i + len(word)
        out.append((start, end))
        i = end + 1
    return out
