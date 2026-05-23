"""react_loop._maybe_recover_tool_call_from_content.

Self-hosted backends (notably vLLM serving Qwen2.5-Coder-Instruct) sometimes
emit a structurally-valid tool-call JSON in the assistant message's `content`
but leave `tool_calls` empty — the chat-template / tool-parser combo failed
to recognise the wrapper tag the model produced. The model did its job;
only the transport-layer extraction failed. The fallback recovers it so the
run keeps moving.

The fallback MUST be invisible to well-behaved backends (Dashscope, OpenAI,
vLLM with a matched parser+template). Most cases below are exercising that
contract — bare JSON / wrapped / fenced shapes get recovered, anything that
looks ambiguous or malformed is left alone.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from graph.nodes.react_loop import _maybe_recover_tool_call_from_content  # noqa: E402


def _msg(content, tool_calls=None):
    """Minimal LangChain-shaped assistant message stub.

    Real LangChain AIMessage has the same two attributes; using a
    SimpleNamespace keeps these tests independent of the langchain version.
    """
    return SimpleNamespace(content=content, tool_calls=tool_calls or [])


# ── well-behaved backends: strict no-op ──────────────────────────────────────


def test_noop_when_tool_calls_already_populated():
    """If the backend already produced tool_calls, the fallback must not
    touch them — otherwise we'd overwrite a real call with a parse of the
    optional `content` (which some backends still set alongside)."""
    existing = [{"name": "submit_fix", "id": "x", "args": {}, "type": "tool_call"}]
    msg = _msg(content='{"name":"abort_fix","arguments":{}}', tool_calls=list(existing))
    n = _maybe_recover_tool_call_from_content(msg)
    assert n == 0
    assert msg.tool_calls == existing


def test_noop_when_content_is_none():
    msg = _msg(content=None)
    assert _maybe_recover_tool_call_from_content(msg) == 0
    assert msg.tool_calls == []


def test_noop_when_content_is_empty():
    msg = _msg(content="")
    assert _maybe_recover_tool_call_from_content(msg) == 0
    assert msg.tool_calls == []


def test_noop_when_content_is_not_json():
    msg = _msg(content="I think the bug is on line 12 — let me look closer.")
    assert _maybe_recover_tool_call_from_content(msg) == 0
    assert msg.tool_calls == []


# ── recovery: shapes self-hosted backends actually emit ──────────────────────


def test_recovers_bare_json_object():
    """The exact shape vLLM + Qwen2.5-Coder-Instruct + hermes parser emits
    when the chat template doesn't apply the <tool_call>…</tool_call>
    wrapper the parser expects."""
    msg = _msg(content='{"name": "submit_fix", "arguments": {"fixes": [{"file_path": "calc.py"}]}}')
    n = _maybe_recover_tool_call_from_content(msg)
    assert n == 1
    [tc] = msg.tool_calls
    assert tc["name"] == "submit_fix"
    assert tc["args"] == {"fixes": [{"file_path": "calc.py"}]}
    assert tc["type"] == "tool_call"
    assert tc["id"].startswith("recovered_")


def test_recovers_from_tool_call_wrapper():
    """A backend that DID emit the <tool_call>…</tool_call> wrapper but the
    vLLM parser still left tool_calls empty (observed when the parser
    version doesn't match the chat-template version)."""
    msg = _msg(content='<tool_call>\n{"name": "abort_fix", "arguments": {"reason": "stuck"}}\n</tool_call>')
    assert _maybe_recover_tool_call_from_content(msg) == 1
    [tc] = msg.tool_calls
    assert tc["name"] == "abort_fix"
    assert tc["args"] == {"reason": "stuck"}


def test_recovers_from_tools_wrapper():
    """The first shape vLLM emitted before we set --chat-template — the
    Qwen2.5 native template echoes the system-prompt's <tools>…</tools>
    framing back into the assistant output."""
    msg = _msg(content='<tools>\n{"name": "ping", "arguments": {}}\n</tools>')
    assert _maybe_recover_tool_call_from_content(msg) == 1
    [tc] = msg.tool_calls
    assert tc["name"] == "ping"
    assert tc["args"] == {}


def test_recovers_from_markdown_fence():
    """Some chat templates wrap the tool output in a ```json fence."""
    msg = _msg(content='```json\n{"name": "submit_fix", "arguments": {}}\n```')
    assert _maybe_recover_tool_call_from_content(msg) == 1
    assert msg.tool_calls[0]["name"] == "submit_fix"


def test_recovers_first_of_array():
    """react_loop processes only tool_calls[0] per turn anyway — taking the
    first call from an array is consistent with the structured-tool_calls
    path's behaviour."""
    msg = _msg(content='[{"name":"fetch_additional_file","arguments":{"path":"a.py"}},{"name":"x","arguments":{}}]')
    assert _maybe_recover_tool_call_from_content(msg) == 1
    [tc] = msg.tool_calls
    assert tc["name"] == "fetch_additional_file"
    assert tc["args"] == {"path": "a.py"}


def test_recovers_with_arguments_as_json_string():
    """OpenAI's wire format double-encodes arguments as a JSON string. Be
    permissive — some self-hosted backends mimic OpenAI verbatim."""
    msg = _msg(content='{"name": "submit_fix", "arguments": "{\\"fixes\\": []}"}')
    assert _maybe_recover_tool_call_from_content(msg) == 1
    assert msg.tool_calls[0]["args"] == {"fixes": []}


def test_recovers_with_args_key_instead_of_arguments():
    """LangChain-style messages use `args`; tolerate either spelling so
    debugging traces / replays don't break recovery."""
    msg = _msg(content='{"name": "abort_fix", "args": {"reason": "nope"}}')
    assert _maybe_recover_tool_call_from_content(msg) == 1
    assert msg.tool_calls[0]["args"] == {"reason": "nope"}


# ── malformed inputs: silent noop, never break the run ───────────────────────


def test_noop_when_name_missing():
    msg = _msg(content='{"arguments": {"x": 1}}')
    assert _maybe_recover_tool_call_from_content(msg) == 0
    assert msg.tool_calls == []


def test_noop_when_arguments_missing():
    """Without args we can't reconstruct the call — refusing to fabricate
    `{}` keeps us honest about what the model actually said."""
    msg = _msg(content='{"name": "submit_fix"}')
    assert _maybe_recover_tool_call_from_content(msg) == 0
    assert msg.tool_calls == []


def test_noop_when_arguments_is_unparseable_string():
    msg = _msg(content='{"name": "x", "arguments": "not json {"}')
    assert _maybe_recover_tool_call_from_content(msg) == 0
    assert msg.tool_calls == []


def test_noop_when_arguments_is_not_an_object():
    """arguments must be a dict (the tool input schema) — a scalar / list
    here is a model hallucination, not a tool call."""
    msg = _msg(content='{"name": "x", "arguments": [1, 2, 3]}')
    assert _maybe_recover_tool_call_from_content(msg) == 0


def test_noop_when_json_is_a_scalar():
    msg = _msg(content='"hello"')
    assert _maybe_recover_tool_call_from_content(msg) == 0


def test_noop_when_array_is_empty():
    msg = _msg(content="[]")
    assert _maybe_recover_tool_call_from_content(msg) == 0


# ── chain-of-thought prose + JSON (the real Qwen2.5-Coder shape) ─────────────


def test_recovers_after_chain_of_thought_prose():
    """The actual content observed from Qwen2.5-Coder-Instruct on the first
    sweep: a paragraph of reasoning, then the tool-call JSON on its own
    line. Anchoring on text.startswith('{') would miss this — the function
    must scan to the last balanced {...} block."""
    content = (
        "To resolve the CI failure, we need to understand why the "
        "`get_last_n` function is returning `[1, 2, 3]` instead of `[]` "
        "when called with `n=0`. The issue likely lies in the "
        "implementation of the `get_last_n` function, which is imported "
        "from `last_n.py`.\n\n"
        "Let's fetch the `last_n.py` file to inspect the implementation "
        "of `get_last_n`.\n\n"
        '{"name": "fetch_additional_file", "arguments": {"path": "last_n.py"}}'
    )
    msg = _msg(content=content)
    assert _maybe_recover_tool_call_from_content(msg) == 1
    [tc] = msg.tool_calls
    assert tc["name"] == "fetch_additional_file"
    assert tc["args"] == {"path": "last_n.py"}


def test_recovers_fenced_json_after_prose():
    """Variant where the model wraps its final answer in a ```json fence
    after a paragraph of reasoning."""
    content = (
        "I'll look at the helper module first.\n\n"
        "```json\n"
        '{"name": "fetch_additional_file", "arguments": {"path": "h.py"}}\n'
        "```"
    )
    msg = _msg(content=content)
    assert _maybe_recover_tool_call_from_content(msg) == 1
    assert msg.tool_calls[0]["name"] == "fetch_additional_file"


def test_takes_last_when_multiple_json_objects():
    """The model sometimes shows a hypothetical example, THEN the actual
    call. Last-balanced-object policy makes the final one win."""
    content = (
        "I could call it like this:\n"
        '{"name": "fetch_additional_file", "arguments": {"path": "wrong.py"}}\n'
        "but actually I want:\n"
        '{"name": "submit_fix", "arguments": {"fixes": []}}'
    )
    msg = _msg(content=content)
    assert _maybe_recover_tool_call_from_content(msg) == 1
    assert msg.tool_calls[0]["name"] == "submit_fix"


def test_handles_braces_inside_string_literals():
    """The brace-walking scanner must respect JSON string state so that a
    `{` or `}` inside a string value doesn't fool it into stopping early."""
    content = (
        "Reasoning...\n"
        '{"name": "submit_fix", "arguments": {"comment": "use { and } here", "n": 1}}'
    )
    msg = _msg(content=content)
    assert _maybe_recover_tool_call_from_content(msg) == 1
    assert msg.tool_calls[0]["name"] == "submit_fix"
    assert msg.tool_calls[0]["args"]["comment"] == "use { and } here"


def test_noop_when_prose_has_no_tool_call_json():
    """Pure prose with no JSON anywhere — the function must NOT fabricate a
    call. Returning 0 sends us down the existing 'nudge the model' path."""
    msg = _msg(content="I don't know how to fix this. Let me think more.")
    assert _maybe_recover_tool_call_from_content(msg) == 0
    assert msg.tool_calls == []


def test_noop_when_only_non_tool_json_in_prose():
    """A JSON object embedded in prose that ISN'T a tool call (missing the
    `name` field) must not synthesise a fake call."""
    msg = _msg(content='Consider this data: {"value": 42, "ok": true} — interesting.')
    assert _maybe_recover_tool_call_from_content(msg) == 0
    assert msg.tool_calls == []


def test_unique_ids_across_recoveries():
    """Synthesised ids should be unique so retry feedback / tool result
    routing in the conversation history can't collide."""
    m1 = _msg(content='{"name":"ping","arguments":{}}')
    m2 = _msg(content='{"name":"ping","arguments":{}}')
    _maybe_recover_tool_call_from_content(m1)
    _maybe_recover_tool_call_from_content(m2)
    assert m1.tool_calls[0]["id"] != m2.tool_calls[0]["id"]
