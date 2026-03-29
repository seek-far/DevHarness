import re
from pathlib import Path
import logging
import sys

logger = logging.getLogger("bf_agent")

def parse_trace(trace_text: str):
    trace_lines = trace_text.split('\n')
    trace_line_cnt = len(trace_lines)
    
    error_i = -1
    for (trace_i, trace_line) in enumerate(trace_lines):
        match_res = re.match(r"E\s+([\w\.]+Error: .+)", trace_line)
        if match_res is not None:
            error_i = trace_i
            break
    if error_i == -1:        
        return {
            "error_message": None,
            "suspect_files": None
        }
    
    error_message = "\n".join(trace_lines[ max(0, error_i-5) : min(trace_line_cnt, error_i+10) ]) # -5 ~ + 10
    logger.debug(f"error_message={error_message}")
    match_res = None
    for trace_line in trace_lines[error_i+1:] :
        match_res = re.match(r'([A-Za-z0-9_/\\.-]+\.py):(\d+)', trace_line)
        if match_res is not None:
            break
    
    if match_res is not None:
        #logger.debug(f"error_file_path={match_res.group(2)}")
        suspect_files = [ { 'file_path': match_res.group(1), 'line_number_1_based': int(match_res.group(2)) } ]    
    else:
        suspect_files = None
    return {
        "error_message": error_message,
        "suspect_files": suspect_files
    }
        
            
# def parse_trace(trace_text: str):
#     # 1. Extract error messages
#     error_msgs = re.findall(r"E\s+([\w\.]+Error: .+)", trace_text)
#     error_msg = error_msgs[-1] if error_msgs else None
#
#     # 2. Extract possible bug file paths
#     file_pattern = re.compile(r'([A-Za-z0-9_/\\.-]+\.py):(\d+)')
#     files = file_pattern.findall(trace_text)
#
#     # Keep only project path files (exclude site-packages or /usr/local/lib)
#     project_files = [
#         f"{path}:{line}" for path, line in files
#         if 'site-packages' not in path and 'usr/local/lib' not in path
#     ]
#     project_files = sorted(set(project_files))
# 
#     return {
#         "error_message": error_msg,
#         "suspect_files": project_files
#     }


# Example usage
if __name__ == "__main__":
    trace_file = Path("/Agent/CI_agent/job_trace.txt")
    trace_text = trace_file.read_text(encoding="utf-8", errors="ignore")
    result = parse_trace(trace_text)
    logger.debug("🧩 Error message:")
    logger.debug(result["error_message"])
    logger.debug("\n🐞 Possible bug files:")
    for f in result["suspect_files"]:
        logger.debug(f)
