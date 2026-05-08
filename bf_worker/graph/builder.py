"""
builder.py — assembles and compiles the BugFix LangGraph StateGraph.

Usage:
    from graph.builder import build_graph
    graph = build_graph()
    final_state = graph.invoke(initial_state)
"""

from __future__ import annotations

from langgraph.graph import StateGraph, END

from graph.state import BugFixState
from graph.routing import (
    route_after_precheck,
    route_after_parse_trace,
    route_after_react_loop,
    route_after_create_fix_branch,
    route_after_apply_and_test,
    route_after_ci,
)
from graph.nodes.precheck_already_fixed import precheck_already_fixed
from graph.nodes.fetch_trace           import fetch_trace
from graph.nodes.parse_trace           import parse_trace
from graph.nodes.fetch_source_file     import fetch_source_file
from graph.nodes.react_loop            import react_loop
from graph.nodes.create_fix_branch     import create_fix_branch
from graph.nodes.apply_change_and_test import apply_change_and_test
from graph.nodes.commit_change         import commit_change
from graph.nodes.wait_ci_result        import wait_ci_result
from graph.nodes.create_mr             import create_mr
from graph.nodes.handle_failure        import handle_failure


def build_graph(checkpointer=None) -> StateGraph:
    """Compile the BugFix graph.

    `checkpointer`: optional LangGraph checkpoint saver. When supplied, every
    node-boundary state update is persisted via thread_id (= bug_id), so a
    crash + restart can resume at the next un-completed node instead of
    re-running fetch_trace + react_loop. None = no checkpointing
    (pre-checkpoint behavior, kept available for tests and standalone runs).
    """
    g = StateGraph(BugFixState)

    # ── register nodes ────────────────────────────────────────────────────────
    g.add_node("precheck_already_fixed", precheck_already_fixed)
    g.add_node("fetch_trace",            fetch_trace)
    g.add_node("parse_trace",            parse_trace)
    g.add_node("fetch_source_file",      fetch_source_file)
    g.add_node("react_loop",             react_loop)
    g.add_node("create_fix_branch",      create_fix_branch)
    g.add_node("apply_change_and_test",  apply_change_and_test)
    g.add_node("commit_change",          commit_change)
    g.add_node("wait_ci_result",         wait_ci_result)
    g.add_node("create_mr",              create_mr)
    g.add_node("handle_failure",         handle_failure)

    # ── entry point ───────────────────────────────────────────────────────────
    # precheck runs first — if R10 hits we exit before any clone or LLM work.
    g.set_entry_point("precheck_already_fixed")

    # ── unconditional edges ───────────────────────────────────────────────────
    g.add_edge("fetch_trace",       "parse_trace")
    g.add_edge("fetch_source_file", "react_loop")
    g.add_edge("commit_change",     "wait_ci_result")
    g.add_edge("create_mr",         END)
    g.add_edge("handle_failure",    END)

    # ── conditional edges ─────────────────────────────────────────────────────
    g.add_conditional_edges(
        "precheck_already_fixed",
        route_after_precheck,
        {
            "fetch_trace":   "fetch_trace",
            "already_fixed": END,
        },
    )

    g.add_conditional_edges(
        "parse_trace",
        route_after_parse_trace,
        {
            "fetch_source_file": "fetch_source_file",
            "react_loop":        "react_loop",   # fallback: parser found no path
        },
    )

    g.add_conditional_edges(
        "react_loop",
        route_after_react_loop,
        {
            "create_fix_branch":     "create_fix_branch",
            "apply_change_and_test": "apply_change_and_test",   # branch-reuse path
            "handle_failure":        "handle_failure",
        },
    )

    g.add_conditional_edges(
        "create_fix_branch",
        route_after_create_fix_branch,
        {
            "apply_change_and_test": "apply_change_and_test",
            "already_fixed":         END,
        },
    )

    g.add_conditional_edges(
        "apply_change_and_test",
        route_after_apply_and_test,
        {
            "commit_change":  "commit_change",
            "react_loop":     "react_loop",
            "handle_failure": "handle_failure",
        },
    )

    g.add_conditional_edges(
        "wait_ci_result",
        route_after_ci,
        {
            "create_mr":      "create_mr",
            "handle_failure": "handle_failure",
        },
    )

    return g.compile(checkpointer=checkpointer) if checkpointer is not None else g.compile()
