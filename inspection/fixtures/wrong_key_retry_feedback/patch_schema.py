def make_fix(line_number, original_line, new_line):
    """The canonical fix-entry schema produced everywhere in the system."""
    return {
        "line_number": line_number,
        "original_line": original_line,
        "new_line": new_line,
    }
