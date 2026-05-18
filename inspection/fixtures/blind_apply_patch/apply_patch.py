from pathlib import Path
import logging

logger = logging.getLogger("bf_agent")


def apply_change_infos(src_filepath: str, change_infos: list[dict]):
    src_filepath = Path(src_filepath)
    src_lines = src_filepath.read_text().split('\n')
    for change_info in change_infos:
        line_number = change_info['line_number']
        original_line = change_info['original_line']
        new_line = change_info['new_line']
        src_lines[line_number - 1] = new_line

    src_filepath.write_text("\n".join(src_lines), encoding="utf-8")
