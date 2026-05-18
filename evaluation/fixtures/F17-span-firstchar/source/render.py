from tokens import spans


def render(text):
    return " ".join("[" + text[s:e] + "]" for s, e in spans(text))
