"""Tests for the workflow_ver == 99 SWE-bench substrate path.

Covers the flag-isolated additions only (ver==0 behaviour is exercised by the
existing suite and must stay byte-identical):
  1. Routing: fetch_trace fork, mini_react_loop fork, apply-node ver99 no-retry.
  2. mini_react_loop node: reads .swebench/instance.json from the instance
     branch via provider.fetch_file(ref=...), runs mini (monkeypatched — no
     docker / LLM), folds model_patch + telemetry into state; empty patch and
     missing descriptor route to handle_failure.
  3. apply_change_and_test ver99: git-applies model_patch to a real temp repo
     and SKIPS the venv/pytest (no .venv created); a non-applying diff fails.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bf_worker"))

from agents.base import FixOutput  # noqa: E402
from graph.routing import (  # noqa: E402
    route_after_fetch_trace,
    route_after_mini_react_loop,
    route_after_apply_and_test,
)
from graph.nodes import mini_react_loop as mrl_mod  # noqa: E402
from graph.nodes.mini_react_loop import mini_react_loop, _instance_id_from_branch  # noqa: E402
from graph.nodes.apply_change_and_test import apply_change_and_test  # noqa: E402


# ── 1. routing ──────────────────────────────────────────────────────────────

def test_fetch_trace_fork():
    assert route_after_fetch_trace({"workflow_ver": 99}) == "mini_react_loop"
    assert route_after_fetch_trace({"workflow_ver": 0}) == "parse_trace"
    assert route_after_fetch_trace({}) == "parse_trace"            # absent → legacy
    assert route_after_fetch_trace({"workflow_ver": "99"}) == "mini_react_loop"  # str coerced


def test_mini_react_loop_fork():
    assert route_after_mini_react_loop({"model_patch": "diff --git ..."}) == "create_fix_branch"
    assert route_after_mini_react_loop({"model_patch": None}) == "handle_failure"
    assert route_after_mini_react_loop({}) == "handle_failure"


def test_apply_route_ver99_no_retry():
    # ver99 failed apply → handle_failure directly (no react_loop retry loop),
    # even though fix_retry_count is under the ver0 cap.
    assert route_after_apply_and_test(
        {"workflow_ver": 99, "test_passed": False, "fix_retry_count": 0}
    ) == "handle_failure"
    # ver99 pass → code_review (same as ver0)
    assert route_after_apply_and_test(
        {"workflow_ver": 99, "test_passed": True}
    ) == "code_review"
    # ver0 failed apply still retries
    assert route_after_apply_and_test(
        {"workflow_ver": 0, "test_passed": False, "fix_retry_count": 0}
    ) == "react_loop"


# ── 2. mini_react_loop node ─────────────────────────────────────────────────

class _Provider:
    """Serves .swebench/instance.json from a given ref; records the ref asked."""
    def __init__(self, payload, raise_exc=None):
        self._payload = payload
        self._raise = raise_exc
        self.asked_ref = None

    def fetch_file(self, file_path, ref="main"):
        self.asked_ref = ref
        if self._raise:
            raise self._raise
        assert file_path == ".swebench/instance.json"
        return json.dumps(self._payload)


def _cfg(provider):
    return {"configurable": {"provider": provider}}


def test_mini_node_success(monkeypatch):
    instance = {"instance_id": "sympy__sympy-22914", "problem_statement": "fix Min/Max"}
    provider = _Provider(instance)

    def fake_run(inst, bug_id):
        assert inst["instance_id"] == "sympy__sympy-22914"
        return FixOutput(
            outcome="fixed", bug_id=bug_id, iterations=5,
            final_state={
                "model_patch": "diff --git a/x b/x\n",
                "llm_call_count": 5, "total_prompt_tokens": 4000,
                "total_completion_tokens": 2000, "total_cost_usd": 0.0012,
                "cost_source": "mini_litellm",
            },
        )
    monkeypatch.setattr(mrl_mod, "run_mini_for_instance", fake_run)

    out = mini_react_loop(
        {"bug_id": "BUG-1", "source_branch": "instance/sympy__sympy-22914", "workflow_ver": 99},
        _cfg(provider),
    )
    assert provider.asked_ref == "instance/sympy__sympy-22914"   # read from the instance branch
    assert out["model_patch"] == "diff --git a/x b/x"            # stored stripped; apply re-adds newline
    assert out["swebench_instance_id"] == "sympy__sympy-22914"
    assert out["react_step_count"] == 5
    assert out["llm_call_count"] == 5
    assert out["total_cost_usd"] == 0.0012
    assert out["cost_source"] == "mini_litellm"


def test_mini_node_empty_patch_routes_to_failure(monkeypatch):
    provider = _Provider({"instance_id": "django__django-1", "problem_statement": "x"})
    monkeypatch.setattr(
        mrl_mod, "run_mini_for_instance",
        lambda inst, bug_id: FixOutput(outcome="no_fix", bug_id=bug_id,
                                       final_state={"model_patch": "   "}),
    )
    out = mini_react_loop(
        {"bug_id": "B", "source_branch": "instance/django__django-1", "workflow_ver": 99},
        _cfg(provider),
    )
    assert out["model_patch"] is None
    assert route_after_mini_react_loop(out) == "handle_failure"


def test_mini_node_missing_descriptor(monkeypatch):
    provider = _Provider(None, raise_exc=RuntimeError("404 file not found"))
    out = mini_react_loop(
        {"bug_id": "B", "source_branch": "instance/foo", "workflow_ver": 99},
        _cfg(provider),
    )
    assert out["model_patch"] is None
    assert "unavailable" in out["error"]
    assert route_after_mini_react_loop(out) == "handle_failure"


def test_mini_node_run_crash(monkeypatch):
    provider = _Provider({"instance_id": "x__y-1", "problem_statement": "z"})

    def boom(inst, bug_id):
        raise RuntimeError("docker daemon unreachable")
    monkeypatch.setattr(mrl_mod, "run_mini_for_instance", boom)

    out = mini_react_loop(
        {"bug_id": "B", "source_branch": "instance/x__y-1", "workflow_ver": 99},
        _cfg(provider),
    )
    assert out["model_patch"] is None
    assert out["swebench_instance_id"] == "x__y-1"
    assert "mini run failed" in out["error"]


def test_instance_id_from_branch():
    assert _instance_id_from_branch("instance/sympy__sympy-22914") == "sympy__sympy-22914"
    assert _instance_id_from_branch("main") == "main"


# ── fetch_trace tolerates a missing job (ver99 never has/uses a CI trace) ─────

def test_ci_wait_timeout_env_override(monkeypatch):
    from graph.nodes.wait_ci_result import _ci_timeout, _DEFAULT_CI_TIMEOUT_S

    monkeypatch.delenv("BF_CI_WAIT_TIMEOUT", raising=False)
    assert _ci_timeout() == _DEFAULT_CI_TIMEOUT_S            # default byte-identical
    monkeypatch.setenv("BF_CI_WAIT_TIMEOUT", "1200")
    assert _ci_timeout() == 1200
    monkeypatch.setenv("BF_CI_WAIT_TIMEOUT", "0")            # non-positive → default
    assert _ci_timeout() == _DEFAULT_CI_TIMEOUT_S
    monkeypatch.setenv("BF_CI_WAIT_TIMEOUT", "nope")         # unparseable → default
    assert _ci_timeout() == _DEFAULT_CI_TIMEOUT_S


def test_fetch_trace_empty_job_id_skips_provider():
    from graph.nodes.fetch_trace import fetch_trace

    class _ProviderExplodes:
        def fetch_trace(self, **kw):
            raise AssertionError("provider.fetch_trace must NOT be called for an empty job_id")

    cfg = {"configurable": {"provider": _ProviderExplodes()}}
    # empty job_id → empty trace, no provider call, no 400 on `/jobs//trace`
    out = fetch_trace({"project_id": "20", "job_id": ""}, cfg)
    assert out == {"trace": "", "fetch_trace_retries": 0}
    # missing project_id too
    out2 = fetch_trace({"job_id": "1701"}, cfg)
    assert out2["trace"] == ""


# ── 3. apply_change_and_test ver99 ──────────────────────────────────────────

class _RepoProvider:
    def __init__(self, repo_path):
        self._repo_path = repo_path

    def ensure_repo_ready(self, bug_id):
        return self._repo_path


def _init_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hello.py").write_text("def greet():\n    return 'hi'\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    return repo


def test_ver99_apply_success_skips_pytest(tmp_path):
    repo = _init_repo(tmp_path)
    # a valid unified diff against hello.py
    patch = (
        "diff --git a/hello.py b/hello.py\n"
        "--- a/hello.py\n"
        "+++ b/hello.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def greet():\n"
        "-    return 'hi'\n"
        "+    return 'hello'\n"
    )
    state = {"bug_id": "B", "workflow_ver": 99, "model_patch": patch, "fix_retry_count": 0}
    out = apply_change_and_test(state, {"configurable": {"provider": _RepoProvider(repo)}})

    assert out["test_passed"] is True
    assert out["apply_error"] is None
    assert "return 'hello'" in (repo / "hello.py").read_text()
    assert not (repo / ".venv").exists()          # local test env NOT created on this path


def test_ver99_apply_failure(tmp_path):
    repo = _init_repo(tmp_path)
    # diff references a line that doesn't exist → will not apply
    patch = (
        "diff --git a/hello.py b/hello.py\n"
        "--- a/hello.py\n"
        "+++ b/hello.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def greet():\n"
        "-    return 'NONEXISTENT'\n"
        "+    return 'x'\n"
    )
    state = {"bug_id": "B", "workflow_ver": 99, "model_patch": patch, "fix_retry_count": 0}
    out = apply_change_and_test(state, {"configurable": {"provider": _RepoProvider(repo)}})

    assert out["test_passed"] is False
    assert out["apply_error"]
    assert route_after_apply_and_test({**state, **out}) == "handle_failure"


def test_ver99_apply_empty_patch(tmp_path):
    repo = _init_repo(tmp_path)
    state = {"bug_id": "B", "workflow_ver": 99, "model_patch": "", "fix_retry_count": 0}
    out = apply_change_and_test(state, {"configurable": {"provider": _RepoProvider(repo)}})
    assert out["test_passed"] is False
    assert "empty model_patch" in out["apply_error"]


def test_mini_telemetry_keys_are_all_declared_in_the_state_schema():
    """Every key mini_react_loop folds into state MUST exist in BugFixState.

    LangGraph silently drops state updates for keys its TypedDict doesn't
    declare — no error, no log. That is how `cost_source` was lost in flight:
    the node returned "mini_litellm", the schema had no such field, and
    RunRecord fell back to inferring "gateway" for a run litellm had priced
    itself (observed on the ls4900 ver99 runs). A missing field here is
    invisible in unit tests that assert on the node's return value, so pin the
    key list against the schema directly.
    """
    from graph.state import BugFixState

    declared = set(BugFixState.__annotations__)
    missing = sorted(k for k in mrl_mod._MINI_TELEMETRY_KEYS if k not in declared)
    assert not missing, f"mini telemetry keys not carriable by BugFixState: {missing}"
