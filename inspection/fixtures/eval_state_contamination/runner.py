"""Evaluation runner. Each (agent, fixture) CELL is an INDEPENDENT trial:
two cells for the same fixture must not influence each other's outcome."""

import sqlite3

# One process-wide checkpoint DB, shared by every cell in the sweep.
_CHECKPOINT_DB = "/tmp/sweep_checkpoints.sqlite"


def _checkpointer():
    return sqlite3.connect(_CHECKPOINT_DB, check_same_thread=False)


def run_cell(fixture_id, agent):
    saver = _checkpointer()
    # thread_id keys the checkpoint; the graph resumes a saved thread instead
    # of starting fresh when the thread already exists.
    config = {"configurable": {"thread_id": fixture_id, "saver": saver}}
    return agent.invoke({"fixture_id": fixture_id}, config=config)


def sweep(fixtures, agents):
    results = {}
    for fx in fixtures:
        for ag in agents:
            results[(ag.name, fx)] = run_cell(fx, ag)
    return results
