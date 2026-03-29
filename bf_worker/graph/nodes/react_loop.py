"""
Node: react_loop

Replaces ask_llm. Runs a ReAct (Reason + Act) loop:

  1. Build an initial prompt with error_info + suspect file content.
  2. Call the LLM with a tool schema.
  3. If the LLM calls a fetch tool  → execute it, append result, loop back.
  4. If the LLM calls submit_fix    → extract llm_result, break.
  5. If the LLM calls abort_fix     → set llm_result=None, break.
  6. If MAX_STEPS reached           → set llm_result=None, break.

Output contract (identical to old ask_llm node):
    llm_result: dict | None
        None  → route_after_react_loop sends to handle_failure
        dict  → same {can_fix, error_reason, step_by_step_thinking, fixes}
                shape that apply_change_and_test already expects
"""

from __future__ import annotations
import json
import logging
import sys
from pathlib import Path

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage

sys.path.append(str(Path(__file__).resolve().parents[3]))
from settings import worker_cfg as cfg

from graph.state import BugFixState
from services.react_tools import TOOLS_SCHEMA, execute_tool

logger = logging.getLogger(__name__)

MAX_STEPS = 8   # max LLM calls per react_loop invocation

llm = ChatOpenAI(
    api_key=cfg.llm_api_key,
    base_url=cfg.llm_api_base_url,
    model=cfg.llm_model,
    temperature=0,
).bind_tools(TOOLS_SCHEMA)

_SYSTEM = """\
You are a Python bug fix agent. Your goal is to find the ROOT CAUSE of a CI \
failure and produce the minimal correct fix.

You have tools to fetch additional source files when needed.

Workflow:
1. Analyse the error_info and the suspect file already provided.
2. If you need more context (e.g. model definitions, imports, callers) use \
fetch_additional_file or fetch_file_segment.
3. When you are confident you understand the root cause, call submit_fix.
4. If you genuinely cannot fix the bug, call abort_fix with a clear reason.

Rules:
- Do NOT guess. Understand the root cause before submitting a fix.
- Each entry in fixes must match a verbatim line from the file.
- Prefer the smallest change that resolves the root cause.\
"""


# ── prompt builders ───────────────────────────────────────────────────────────

def _build_initial_messages(state: BugFixState) -> list:
    user_content = (
        "## CI failure info\n"
        f"{state['error_info']}\n\n"
        f"## Suspect file: {state['suspect_file_path']}\n"
        "```python\n"
        f"{state['source_file_content']}\n"
        "```"
    )
    return [
        SystemMessage(content=_SYSTEM),
        HumanMessage(content=user_content),
    ]


# ── main node ─────────────────────────────────────────────────────────────────

def react_loop(state: BugFixState) -> BugFixState:
    logger.info("react_loop: start  bug_id=%s", state.get("bug_id"))

    messages      = _build_initial_messages(state)
    step_count    = 0
    llm_result    = None
    confidence    = None
    reasoning     = None
    tool_call_log: list[dict] = []

    while step_count < MAX_STEPS:

        # ── call LLM ──────────────────────────────────────────────────────────
        logger.info("react_loop: step %d — calling LLM", step_count + 1)
        assistant_msg = llm.invoke(messages)
        step_count += 1

        # Append the full assistant message (including tool_calls field)
        # so the next LLM call has a valid conversation history.
        messages.append(assistant_msg)

        tool_calls = assistant_msg.tool_calls
        if not tool_calls:
            # LLM returned plain text instead of calling a tool.
            # Nudge it back on track and continue.
            logger.warning(
                "react_loop: step %d — LLM returned text without tool call, nudging",
                step_count,
            )
            messages.append(HumanMessage(content=(
                "You must call one of the provided tools. "
                "If you have identified the fix, call submit_fix. "
                "If you cannot fix the bug, call abort_fix."
            )))
            continue

        # Take the first tool call (schema is designed for one call per turn)
        tc         = tool_calls[0]
        tool_name  = tc["name"]
        tool_id    = tc["id"]
        tool_input = tc["args"]  # already a dict, no JSON parsing needed

        logger.info("react_loop: step %d — tool=%s", step_count, tool_name)
        tool_call_log.append({
            "step":  step_count,
            "tool":  tool_name,
            "input": tool_input,
        })

        # ── handle terminal tools ─────────────────────────────────────────────
        if tool_name == "submit_fix":
            fixes = tool_input.get("fixes", [])
            if not fixes:
                logger.warning("react_loop: submit_fix called with empty fixes list")
                # Treat as abort
                break

            llm_result = {
                "can_fix":               True,
                "error_reason":          tool_input.get("error_reason", ""),
                "step_by_step_thinking": tool_input.get("reasoning", ""),
                "fixes":                 fixes,
            }
            confidence = tool_input.get("confidence", "medium")
            reasoning  = tool_input.get("reasoning", "")
            logger.info(
                "react_loop: submit_fix — %d fix(es), confidence=%s",
                len(fixes), confidence,
            )
            break

        if tool_name == "abort_fix":
            logger.warning(
                "react_loop: abort_fix — reason=%s", tool_input.get("reason")
            )
            # llm_result stays None → handle_failure
            break

        # ── handle fetch tools ────────────────────────────────────────────────
        tool_result = execute_tool(tool_name, tool_input, state["project_web_url"], state["bug_id"])

        # Append tool result as a ToolMessage (LangChain format)
        messages.append(ToolMessage(
            tool_call_id=tool_id,
            content=tool_result,
        ))

    else:
        # Loop exited without break → MAX_STEPS reached
        logger.warning("react_loop: MAX_STEPS=%d reached without resolution", MAX_STEPS)

    if llm_result is None:
        logger.warning("react_loop: finished with no usable result")
    else:
        logger.info("react_loop: finished successfully")

    return {
        "llm_result":       llm_result,
        "react_step_count": step_count,
        "react_messages":   messages,
        "react_tool_calls": tool_call_log,
        "react_confidence": confidence,
        "react_reasoning":  reasoning,
        # Clear any stale retry state from a previous cycle
        "test_passed":      None,
        "test_output":      None,
        "apply_error":      None,
    }
