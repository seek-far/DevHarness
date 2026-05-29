import re
from pathlib import Path
import logging
import sys

logger = logging.getLogger("bf_agent")


# GitLab runner prefixes every trace line with a timestamp + stream marker.
# Shape: `<ISO 8601 timestamp> <2-digit-stream><O|E>[+] ` — e.g.
#   `2026-05-28T22:02:21.669535Z 01O `   (stdout)
#   `2026-05-28T22:02:21.787558Z 01E `   (stderr)
#   `2026-05-28T22:02:12.634130Z 00O+`   (continuation)
# Without stripping this the `re.match(r"E\s+...")` regexes below never
# fire on GitLab-collected traces because position 0 is the date, not E.
_GITLAB_PREFIX_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\s+\d+[OE]\+?\s*"
)

# ANSI CSI escape: ESC [ <params> <letter>. Catches color codes like
# `\x1b[0K`, `\x1b[0;31m`, `\x1b[36;1m` that GitLab leaves intact in
# captured stdout. Strip so the error/file regexes see the underlying text.
_ANSI_RE = re.compile(r"\x1b\[[\d;]*[a-zA-Z]")


def _strip_trace_prefix(line: str) -> str:
    """Remove GitLab CI's per-line timestamp/stream prefix and any ANSI
    escape codes so the existing regexes (anchored at line start) can
    match the raw pytest output nested inside.

    Idempotent: passing a line that has no prefix returns it unchanged,
    so raw-pytest traces (e.g. from integration_test.py) still parse
    byte-identically to pre-fix behaviour.
    """
    line = _ANSI_RE.sub("", line)
    line = _GITLAB_PREFIX_RE.sub("", line)
    return line


def parse_trace(trace_text: str):
    trace_lines = trace_text.split('\n')
    trace_line_cnt = len(trace_lines)

    error_i = -1
    for (trace_i, trace_line) in enumerate(trace_lines):
        stripped = _strip_trace_prefix(trace_line)
        # Standard exception line:  "E   ZeroDivisionError: ..."
        match_res = re.match(r"E\s+([\w\.]+Error: .+)", stripped)
        # pytest assertion failure:  "E   assert <expr>" — no exception class shown
        if match_res is None:
            match_res = re.match(r"E\s+(assert .+)", stripped)
        if match_res is not None:
            error_i = trace_i
            break
    if error_i == -1:
        return {
            "error_message": None,
            "suspect_files": None
        }

    # error_message keeps the ORIGINAL surrounding lines (with their prefix)
    # so downstream prompt rendering reproduces the trace verbatim and the
    # LLM sees what the operator would see in GitLab. Only the regex needs
    # to see the stripped form.
    error_message = "\n".join(trace_lines[max(0, error_i-5): min(trace_line_cnt, error_i+10)])
    logger.debug(f"error_message={error_message}")
    match_res = None
    for trace_line in trace_lines[error_i+1:]:
        stripped = _strip_trace_prefix(trace_line)
        match_res = re.match(r'([A-Za-z0-9_/\\.-]+\.py):(\d+)', stripped)
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
