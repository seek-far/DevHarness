from pathlib import Path
import shutil
import logging
import sys
import platform

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

if __name__ == '__main__':
    src_filepath='/my_git/restaurant_order_demo__order_be_bf/api/views.py'
    if platform.system() == "Linux":
        src_filepath =  "/mnt/d" + src_filepath
        
    change_infos = [
      {
        "line_number": 6,
        "original_line": "    queryset = Dish.all().order_by('id')",
        "new_line": "    queryset = Dish.objects.all().order_by('id')"
      }
    ]
    apply_change_infos(src_filepath=src_filepath, change_infos=change_infos)   