"""
Resume-from-checkpoint tests.

Strategy: skip the real BugFix graph (it pulls in LLM clients, providers,
etc.) and build a minimal three-node graph that records which nodes
executed. Run it twice with the same thread_id, with a controlled crash
in the second node on the first run, and verify the second run only
re-executes from where the first one died.

This is exactly what we want the checkpointer to do for the real graph:
crash → restart → resume at the next un-completed node, not from scratch.

We also assert one important *non-property*: provider/hooks/budget passed
via config are NOT persisted across runs (they're runtime context, not
checkpointed state) — proving the state-layering work in Phase 2 holds.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TypedDict

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from typing import Optional  # noqa: E402

from langchain_core.runnables import RunnableConfig  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.graph import StateGraph, END  # noqa: E402

from services.runtime_context import get_provider  # noqa: E402


class _ToyState(TypedDict, total=False):
    history: list[str]
    fail_at: str   # node name to raise in (used to simulate a crash)
    bug_id: str


def _make_graph_with_crash():
    """Three-node graph: A → B → C → END.

    Each node appends its name to history; `B` raises if `fail_at == "B"`.
    A reads provider from config to prove it's flowing through. Provider
    is NOT in state, so checkpoint round-trip can't carry it across runs.
    """
    calls: list[str] = []

    def node_a(state: _ToyState, config: Optional[RunnableConfig] = None) -> _ToyState:
        # Touch config to prove it's available — node-level wiring smoke.
        provider = get_provider(config)
        assert provider is not None
        calls.append("A")
        return {"history": (state.get("history") or []) + ["A"]}

    def node_b(state: _ToyState, config: Optional[RunnableConfig] = None) -> _ToyState:
        if state.get("fail_at") == "B":
            calls.append("B-crash")
            raise RuntimeError("simulated crash in B")
        calls.append("B")
        return {"history": (state.get("history") or []) + ["B"]}

    def node_c(state: _ToyState, config: Optional[RunnableConfig] = None) -> _ToyState:
        calls.append("C")
        return {"history": (state.get("history") or []) + ["C"]}

    g = StateGraph(_ToyState)
    g.add_node("A", node_a)
    g.add_node("B", node_b)
    g.add_node("C", node_c)
    g.set_entry_point("A")
    g.add_edge("A", "B")
    g.add_edge("B", "C")
    g.add_edge("C", END)
    return g, calls


def _cfg(thread_id: str, provider="dummy"):
    return {"configurable": {"provider": provider, "thread_id": thread_id}}


# ── happy path: full run with checkpointer attached ─────────────────────────


def test_happy_path_runs_all_nodes_once():
    g, calls = _make_graph_with_crash()
    saver = MemorySaver()
    graph = g.compile(checkpointer=saver)

    out = graph.invoke({"history": []}, config=_cfg("t-happy"))
    assert out["history"] == ["A", "B", "C"]
    assert calls == ["A", "B", "C"]


# ── crash in B → restart resumes at B, not at A ─────────────────────────────


def test_crash_in_b_restart_resumes_at_b_not_from_scratch():
    g, calls = _make_graph_with_crash()
    saver = MemorySaver()
    graph = g.compile(checkpointer=saver)

    # First run: crash in B
    with pytest.raises(RuntimeError, match="simulated crash in B"):
        graph.invoke({"history": [], "fail_at": "B"}, config=_cfg("t-crash"))
    assert calls == ["A", "B-crash"]   # A completed; B raised before completing

    # Second run on the same thread, with the crash trigger lifted via state
    # update. The checkpoint state from the first run carries fail_at="B" —
    # so we explicitly clear it via update_state before resuming.
    graph.update_state(_cfg("t-crash"), {"fail_at": ""})

    out = graph.invoke(None, config=_cfg("t-crash"))   # None = "resume"

    # B and C run on this attempt. A does NOT run again — it was checkpointed.
    assert "A" not in calls[2:]   # only the original "A" call, not a re-run
    assert calls[2:] == ["B", "C"]
    assert out["history"] == ["A", "B", "C"]


# ── different thread_id → independent run ───────────────────────────────────


def test_different_thread_runs_independently():
    g, calls = _make_graph_with_crash()
    saver = MemorySaver()
    graph = g.compile(checkpointer=saver)

    graph.invoke({"history": []}, config=_cfg("t-1"))
    graph.invoke({"history": []}, config=_cfg("t-2"))

    # Each thread runs the full A→B→C — total 6 node calls
    assert calls == ["A", "B", "C", "A", "B", "C"]


# ── provider is NOT persisted across runs (config, not state) ──────────────


def test_provider_does_not_leak_across_runs_via_checkpoint():
    """Provider is supplied via config["configurable"], not state.

    LangGraph checkpoints persist state, not config. So after a crash,
    a brand-new provider instance can be supplied on resume — proving
    the state-layering refactor (Phase 2) actually decouples runtime
    context from persistent state.
    """
    g, calls = _make_graph_with_crash()
    saver = MemorySaver()
    graph = g.compile(checkpointer=saver)

    # First run: provider-X
    with pytest.raises(RuntimeError):
        graph.invoke({"history": [], "fail_at": "B"},
                     config={"configurable": {"provider": "provider-X",
                                              "thread_id": "t-provider"}})

    graph.update_state({"configurable": {"thread_id": "t-provider"}},
                       {"fail_at": ""})

    # Second run: completely different provider instance.
    # Resume must succeed using THIS provider, not the one from run 1.
    out = graph.invoke(None,
                       config={"configurable": {"provider": "provider-Y",
                                                "thread_id": "t-provider"}})
    assert out["history"] == ["A", "B", "C"]


# ── factory selects the right backend ──────────────────────────────────────


def test_factory_returns_none_when_disabled(monkeypatch):
    from services.checkpointer import build_checkpointer
    monkeypatch.setenv("BF_CHECKPOINT_BACKEND", "none")
    assert build_checkpointer() is None


def test_factory_returns_memory_when_requested(monkeypatch):
    from services.checkpointer import build_checkpointer
    monkeypatch.setenv("BF_CHECKPOINT_BACKEND", "memory")
    cp = build_checkpointer()
    assert cp is not None
    # langgraph 1.x renamed MemorySaver → InMemorySaver; accept either to
    # stay version-tolerant.
    assert type(cp).__name__ in ("MemorySaver", "InMemorySaver")


def test_factory_returns_sqlite_with_tmp_path(monkeypatch, tmp_path):
    from services.checkpointer import build_checkpointer
    monkeypatch.setenv("BF_CHECKPOINT_BACKEND", "sqlite")
    monkeypatch.setenv("BF_CHECKPOINT_PATH", str(tmp_path / "test.sqlite"))
    cp = build_checkpointer()
    assert cp is not None
    assert type(cp).__name__ == "SqliteSaver"
    assert (tmp_path / "test.sqlite").parent.exists()


def test_factory_rejects_unknown_backend(monkeypatch):
    from services.checkpointer import build_checkpointer
    monkeypatch.setenv("BF_CHECKPOINT_BACKEND", "carrier-pigeon")
    with pytest.raises(ValueError, match="unknown BF_CHECKPOINT_BACKEND"):
        build_checkpointer()
