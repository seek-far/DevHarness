"""Cache key derivation tests.

The key MUST be invariant to:
  * tool_call_id (random per call)
  * assistant message contents (LLM-generated)
  * tools list reordering
  * params key ordering

The key MUST change when ANY of these change:
  * model name
  * system / user / tool message contents (including their `name`)
  * tools schema
  * sampling params (temperature / top_p / max_tokens / seed)

These two properties together let a ReAct multi-turn flow re-hash to the
same key across replays, because the only differences between runs are
LLM-generated (assistant) content and the SDK-generated tool_call_id.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm_gateway.keying import derive_key, short_key


def _base_request() -> dict:
    return {
        "model": "qwen3-coder-480b",
        "messages": [
            {"role": "system", "content": "You are a bug-fix agent."},
            {"role": "user", "content": "Trace: AssertionError at calc.py:7"},
        ],
        "tools": [
            {"type": "function", "function": {
                "name": "fetch_file",
                "description": "Read a file from the repo",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            }},
            {"type": "function", "function": {
                "name": "submit_fix",
                "description": "Submit the proposed fix",
                "parameters": {"type": "object"},
            }},
        ],
        "temperature": 0.0,
        "stream": False,
    }


# ── invariance properties ───────────────────────────────────────────────────


def test_same_request_same_key():
    assert derive_key(_base_request()) == derive_key(_base_request())


def test_assistant_messages_are_ignored():
    """The whole point of the design: a second-turn request that carries
    the LLM's first-turn response (assistant message) must hash to the
    same key as a first-turn request without it. Otherwise multi-turn
    ReAct loops cache-miss on every turn after the first."""
    a = _base_request()
    b = _base_request()
    # b carries the assistant turn that turn 1 would have produced.
    b["messages"].append({
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call_HzVB7qLkA9JxRt2m",
            "type": "function",
            "function": {"name": "fetch_file",
                         "arguments": '{"path": "calc.py"}'}
        }],
    })
    assert derive_key(a) == derive_key(b)


def test_tool_call_id_does_not_affect_key():
    """tool messages carry `tool_call_id` referencing the assistant's
    randomly-generated call ID. The key must be invariant to it so
    replays don't cache-miss on every second turn."""
    a = _base_request()
    a["messages"].append({
        "role": "tool",
        "tool_call_id": "call_AAA111",
        "name": "fetch_file",
        "content": "def add(a, b): return a + b\n",
    })
    b = _base_request()
    b["messages"].append({
        "role": "tool",
        "tool_call_id": "call_DIFFERENT",   # different random ID
        "name": "fetch_file",
        "content": "def add(a, b): return a + b\n",
    })
    assert derive_key(a) == derive_key(b)


def test_tools_list_order_does_not_affect_key():
    """A config that swaps the order of declared tools should not bust
    the cache — semantic content is the same."""
    a = _base_request()
    b = _base_request()
    b["tools"] = list(reversed(b["tools"]))
    assert derive_key(a) == derive_key(b)


def test_stream_flag_does_not_affect_key():
    """`stream` is a transport detail (chunked vs not). The model output
    distribution is the same; cache key should be too."""
    a = _base_request()
    a["stream"] = False
    b = _base_request()
    b["stream"] = True
    assert derive_key(a) == derive_key(b)


# ── sensitivity properties ──────────────────────────────────────────────────


def test_model_change_changes_key():
    a = _base_request()
    b = _base_request()
    b["model"] = "qwen3-coder-other"
    assert derive_key(a) != derive_key(b)


def test_user_message_change_changes_key():
    a = _base_request()
    b = _base_request()
    b["messages"][1]["content"] = "Trace: AssertionError at calc.py:8"  # different line
    assert derive_key(a) != derive_key(b)


def test_system_message_change_changes_key():
    """System prompt is part of the semantic input. A worker code change
    that adjusts the system prompt should invalidate every cached entry
    that used the old one."""
    a = _base_request()
    b = _base_request()
    b["messages"][0]["content"] = "You are an updated bug-fix agent."
    assert derive_key(a) != derive_key(b)


def test_tool_message_content_change_changes_key():
    a = _base_request()
    b = _base_request()
    for body in (a, b):
        body["messages"].append({"role": "tool", "tool_call_id": "x",
                                 "name": "fetch_file", "content": "v1"})
    b["messages"][-1]["content"] = "v2"
    assert derive_key(a) != derive_key(b)


def test_tool_schema_change_changes_key():
    """If the worker upgrades `submit_fix` schema (new required field),
    old cache entries should miss — replaying them would feed the worker
    an old-shape response that doesn't validate."""
    a = _base_request()
    b = _base_request()
    b["tools"][1]["function"]["parameters"] = {
        "type": "object",
        "properties": {"new_required_field": {"type": "string"}},
        "required": ["new_required_field"],
    }
    assert derive_key(a) != derive_key(b)


def test_temperature_change_changes_key():
    a = _base_request()
    b = _base_request()
    b["temperature"] = 0.7
    assert derive_key(a) != derive_key(b)


def test_seed_change_changes_key():
    a = _base_request()
    a["seed"] = 42
    b = _base_request()
    b["seed"] = 43
    assert derive_key(a) != derive_key(b)


# ── short key contract ─────────────────────────────────────────────────────


def test_short_key_is_12_hex_chars():
    """The 12-char prefix is the log-line contract — analyzers and grep
    commands rely on it."""
    full = derive_key(_base_request())
    s = short_key(full)
    assert len(s) == 12
    assert all(c in "0123456789abcdef" for c in s)
    assert full.startswith(s)
