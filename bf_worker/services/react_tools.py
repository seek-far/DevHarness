"""
services/react_tools.py

Tool schema (passed to LLM) and tool execution functions for the ReAct loop.

Public API:
    TOOLS_SCHEMA          — list[dict] to pass as `tools=` in LLM call
    execute_tool(...)     — dispatches tool_name → result string
"""

from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

# Maximum characters returned for a single file fetch.
# Keeps individual tool results well within context limits.
_MAX_FILE_CHARS = 8000


# ── Tool schema (OpenAI function-calling format) ──────────────────────────────

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "fetch_additional_file",
            "description": (
                "Fetch the full content of a source file from the repository. "
                "Use when you need more context beyond the primary suspect file "
                "(e.g. models.py, serializers.py, urls.py). "
                "The file is fetched from the main branch."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "File path relative to repo root, "
                            "e.g. 'api/models.py' or 'restaurant/settings.py'."
                        ),
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_file_segment",
            "description": (
                "Fetch a specific range of lines from a source file. "
                "Use when you only need a narrow section of a large file "
                "to conserve context. Lines are 1-based."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to repo root.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "First line to fetch, 1-based inclusive.",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Last line to fetch, 1-based inclusive.",
                    },
                },
                "required": ["path", "start_line", "end_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_fix",
            "description": (
                "Submit your fix plan. Call this when you are confident you have "
                "identified the root cause and have a correct fix. "
                "Each entry in 'fixes' is one line replacement."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "error_reason": {
                        "type": "string",
                        "description": "Brief description of the root cause.",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": (
                            "Step-by-step explanation of why this fix resolves "
                            "the root cause."
                        ),
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": (
                            "Your confidence that this fix is correct. "
                            "Use 'low' if you are guessing."
                        ),
                    },
                    "fixes": {
                        "type": "array",
                        "description": "List of line-level changes to apply.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file_path": {
                                    "type": "string",
                                    "description": (
                                        "Repo-relative path of the file to edit. "
                                        "Required when the fix is in a different "
                                        "file from the suspect file (e.g. the test "
                                        "is the suspect but the bug is in an "
                                        "imported module). If omitted, the suspect "
                                        "file is used."
                                    ),
                                },
                                "line_number": {
                                    "type": "integer",
                                    "description": "1-based line number in the file.",
                                },
                                "original_line": {
                                    "type": "string",
                                    "description": (
                                        "Verbatim content of the line to replace "
                                        "(must match exactly)."
                                    ),
                                },
                                "new_line": {
                                    "type": "string",
                                    "description": "Replacement line content.",
                                },
                            },
                            "required": ["line_number", "original_line", "new_line"],
                        },
                    },
                },
                "required": ["error_reason", "reasoning", "confidence", "fixes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "abort_fix",
            "description": (
                "Declare that the bug cannot be fixed automatically. "
                "Call this only when you have genuinely exhausted all options."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Clear explanation of why the bug cannot be fixed.",
                    }
                },
                "required": ["reason"],
            },
        },
    },
]


# ── Tool execution ────────���───────────────────────────���────────────────────────

def execute_tool(tool_name: str, tool_input: dict, provider) -> str:
    """
    Execute a fetch tool and return the result as a plain string.
    submit_fix and abort_fix are handled directly in react_loop — never
    passed here.
    """
    if tool_name == "fetch_additional_file":
        return _fetch_full_file(provider, tool_input["path"])

    if tool_name == "fetch_file_segment":
        return _fetch_segment(
            provider,
            tool_input["path"],
            tool_input["start_line"],
            tool_input["end_line"],
        )

    # Should never reach here during normal operation
    logger.error("execute_tool: unknown tool '%s'", tool_name)
    return f"[error: unknown tool '{tool_name}']"


# ── private helpers ───────────────────────────────────────────��────────────────

def _fetch_full_file(provider, path: str) -> str:
    try:
        content = provider.fetch_file(path)
    except Exception as exc:
        logger.warning("fetch_additional_file failed for '%s': %s", path, exc)
        return f"[error fetching '{path}': {exc}]"

    truncated = ""
    if len(content) > _MAX_FILE_CHARS:
        truncated = f"\n... [truncated — showing first {_MAX_FILE_CHARS} of {len(content)} chars]"
        content = content[:_MAX_FILE_CHARS]

    logger.info("fetch_additional_file: %s (%d chars%s)", path, len(content),
                ", truncated" if truncated else "")
    return f"# File: {path}\n```python\n{content}\n```{truncated}"


def _fetch_segment(provider, path: str, start: int, end: int) -> str:
    try:
        content = provider.fetch_file(path)
    except Exception as exc:
        logger.warning("fetch_file_segment failed for '%s': %s", path, exc)
        return f"[error fetching '{path}': {exc}]"

    lines = content.split("\n")
    total = len(lines)
    # Convert 1-based to 0-based and clamp to valid range
    start0 = max(0, start - 1)
    end0   = min(end - 1, total - 1)

    segment = "\n".join(lines[start0 : end0 + 1])
    logger.info("fetch_file_segment: %s lines %d-%d", path, start, end)
    return (
        f"# File: {path}  (lines {start}–{end} of {total})\n"
        f"```python\n{segment}\n```"
    )
