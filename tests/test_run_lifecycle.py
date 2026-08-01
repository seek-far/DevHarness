"""Who ends a run, and when (W2.5's memo invariant).

The finished-loop record deliberately outlives `fix()` so that a worker killed
during git-apply / push / the CI wait can replay the node's output instead of
re-spending its whole trajectory. That memo is only safe while one sentence
stays true:

    a record exists  ⇒  the previous process did not finish normally

Nothing in the agent can maintain that: the entry point that owns the *run* has
to end it. So these tests are about wiring, and each entry point has a
different correct answer:

  * the GitLab / ver99 worker purges after the graph reaches an end node, and
    deliberately NOT in a `finally` — an exception or a cancellation means the
    run did not finish, which is exactly when the next incarnation wants the
    records;
  * `swebench_single` and `swebench_batch` purge in a `finally`, because they
    key on `instance_id`, which repeats across sweeps by construction
    (project invariant #4's twin). A record left behind there is not a resume
    opportunity, it is contamination of the NEXT sweep of the same instance.

A process killed from outside skips all of them — that is the resume case, and
it is the one path none of these tests can simulate (see
tests/test_mini_resume_docker.py for the real `kill -9`).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "bf_worker"))

from agents.base import FixOutput  # noqa: E402


class _FakeAgent:
    """Stands in for MiniSweAgent: records whether the run was ended."""

    name = "mini_swe_agent"

    def __init__(self, *a, **kw):
        self.finished = 0
        self.raise_on_fix: BaseException | None = None
        _FakeAgent.last = self

    def fix(self, bug_input):
        if self.raise_on_fix is not None:
            raise self.raise_on_fix
        return FixOutput(outcome="fixed", bug_id=bug_input.bug_id, error=None, iterations=3,
                         final_state={"swebench_instance_id": bug_input.bug_id,
                                      "model_patch": "diff --git a/x b/x\n"})

    def finish_run(self):
        self.finished += 1


# ── swebench_batch: a finally, per instance ─────────────────────────────────

@pytest.fixture()
def batch(monkeypatch):
    pytest.importorskip("minisweagent", reason="upstream mini-swe-agent not installed")
    import swebench_batch as B

    monkeypatch.setattr(B, "MiniSweAgent", _FakeAgent)
    return B


def _run_one(B, tmp_path, agent_mutator=None):
    preds = tmp_path / "preds.json"
    preds.write_text("{}")
    records = tmp_path / "records"
    records.mkdir()

    original = _FakeAgent.__init__

    def patched(self, *a, **kw):
        original(self, *a, **kw)
        if agent_mutator:
            agent_mutator(self)

    _FakeAgent.__init__ = patched
    try:
        return B.run_one({"instance_id": "sympy__sympy-1"}, {"agent": {}}, "m", records, preds)
    finally:
        _FakeAgent.__init__ = original


def test_batch_ends_the_run_after_a_successful_instance(batch, tmp_path):
    _run_one(batch, tmp_path)
    assert _FakeAgent.last.finished == 1


def test_batch_ends_the_run_even_when_the_instance_crashes(batch, tmp_path):
    """The dangerous one. A crashed instance whose record survives is inherited
    by the NEXT sweep of the same instance — the key is the instance id, and it
    repeats by construction."""
    def boom(agent):
        agent.raise_on_fix = RuntimeError("mini exploded")

    record = _run_one(batch, tmp_path, agent_mutator=boom)
    assert record["outcome"] == "error"
    assert _FakeAgent.last.finished == 1, "a failed instance must still end its run"


# ── swebench_single: a finally around the fix ───────────────────────────────

@pytest.fixture()
def single(monkeypatch, tmp_path):
    pytest.importorskip("minisweagent", reason="upstream mini-swe-agent not installed")
    import swebench_single as S

    monkeypatch.setattr(S, "MiniSweAgent", _FakeAgent)
    monkeypatch.setattr(S, "load_instance",
                        lambda *a, **k: {"instance_id": "sympy__sympy-1"})
    monkeypatch.setattr(S, "build_mini_config", lambda *a, **k: {"model": {"model_name": "m"}})
    monkeypatch.setattr(S, "grade_with_harness", lambda *a, **k: {"sympy__sympy-1": True})
    monkeypatch.setattr(S.JournalWriter, "write", lambda self, *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["swebench_single", "--instance", "sympy__sympy-1",
                                      "--output-dir", str(tmp_path / "out"), "--no-grade"])
    return S


def test_single_ends_the_run(single):
    single.main()
    assert _FakeAgent.last.finished == 1


def test_single_ends_the_run_when_the_fix_raises(single):
    original = _FakeAgent.__init__

    def patched(self, *a, **kw):
        original(self, *a, **kw)
        self.raise_on_fix = RuntimeError("mini exploded")

    _FakeAgent.__init__ = patched
    try:
        with pytest.raises(RuntimeError):
            single.main()
    finally:
        _FakeAgent.__init__ = original
    assert _FakeAgent.last.finished == 1


# ── bf_worker: after the graph, and NOT in a finally ────────────────────────

class _FakeRedis:
    def __init__(self):
        self.calls = []

    async def setex(self, *a, **k):
        self.calls.append("setex")

    async def set(self, *a, **k):
        self.calls.append("set")

    async def delete(self, *a, **k):
        self.calls.append("delete")

    async def aclose(self):
        self.calls.append("aclose")


def _load_worker_module():
    """Load bf_worker/bf_worker.py by path.

    `import bf_worker` is ambiguous here: the repo has both a *package*
    directory of that name and a module inside it, so which one you get depends
    on what an earlier test already put in sys.modules. These tests passed
    alone and errored in the full suite until this was made explicit.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "bf_worker_entrypoint", _ROOT / "bf_worker" / "bf_worker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def worker(monkeypatch):
    pytest.importorskip("redis", reason="redis client not installed")
    W = _load_worker_module()

    purged: list[str] = []
    monkeypatch.setattr(W, "purge_run_records", lambda key: purged.append(key))

    async def _no_heartbeat(*a, **k):
        await asyncio.sleep(3600)

    monkeypatch.setattr(W, "_heartbeat_loop", _no_heartbeat)
    w = W.BugFixWorker("BUG-1")
    w._redis = _FakeRedis()
    monkeypatch.setattr(w, "_cleanup_repo", lambda: None)
    return w, purged


def test_the_worker_ends_the_run_once_the_graph_finishes(worker, monkeypatch):
    w, purged = worker
    monkeypatch.setattr(w, "_run_graph", lambda: None)
    asyncio.run(w.run())
    assert purged == ["BUG-1"]
    assert "set" in w._redis.calls, "the completion key still gets written"


def test_a_crashing_graph_keeps_the_records(worker, monkeypatch):
    """Deliberately NOT in a `finally`: the run did not finish, so the next
    incarnation is exactly who wants these records. Putting the purge in the
    `finally` — where the completion key correctly lives — would delete them on
    the way out of the very failure resume exists for."""
    def boom():
        raise RuntimeError("graph exploded")

    w, purged = worker
    monkeypatch.setattr(w, "_run_graph", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(w.run())
    assert purged == [], "an unfinished run must keep its records"
    assert "delete" in w._redis.calls, "teardown still ran"
