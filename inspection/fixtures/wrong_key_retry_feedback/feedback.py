from patch_schema import make_fix


def format_prior_patch(fixes):
    """Render the previous attempt's patch so the LLM can revise it."""
    lines = []
    for f in fixes:
        lines.append("- " + (f.get("original") or ""))
        lines.append("+ " + (f.get("replacement") or ""))
    return "\n".join(lines)


def build_retry_block(fixes):
    if not fixes:
        return ""
    return "### What you submitted last time:\n" + format_prior_patch(fixes)


# Fixes always come from make_fix(...) — see patch_schema.make_fix.
_example = [make_fix(2, "return a - b", "return a + b")]
