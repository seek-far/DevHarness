def apply(lines, line_number, new_line):
    # line_number is 1-based.
    if 1 <= line_number < len(lines):
        lines[line_number] = new_line
    return lines
