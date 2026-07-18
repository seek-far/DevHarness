"""MiniSweAgent adapter — the mini-swe-agent SWE-bench substrate bridge.

These tests exercise the adapter with mini's bundled DeterministicModel + a
LocalEnvironment, so CI needs NO docker, NO LLM, and NO network. They pin:

  - submission-sentinel → outcome="fixed" + patch captured in final_state;
  - limit-exhausted (no submission) → outcome="no_fix";
  - an uncaught model error → outcome="error";
  - the gateway model-config wiring (replay-cache/cost reuse by config);
  - the additive RunRecord SWE-bench fields (swebench_instance_id / resolved)
    and the mini_litellm cost_source path.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "bf_worker"))

from agents.base import BugInput  # noqa: E402
from agents.mini_swe_agent import MiniSweAgent, build_gateway_model_config  # noqa: E402
from agents.run_record import RunRecord  # noqa: E402

from minisweagent.environments.local import LocalEnvironment  # noqa: E402
from minisweagent.models.test_models import DeterministicModel, make_output  # noqa: E402


_INSTANCE = {
    "instance_id": "astropy__astropy-12345",
    "problem_statement": "The frobnicator returns the wrong value for empty input.",
}

# mini's AgentConfig requires these two templates; keep them trivial for tests.
_AGENT_CFG = {
    "system_template": "You are a helpful assistant.",
    "instance_template": "{{task}}",
    "step_limit": 5,
}

# Command whose stdout begins with the submission sentinel; the environment
# turns this into a Submitted with everything after line 1 as the patch.
_SUBMIT_PATCH = "diff --git a/x.py b/x.py\n+    return 0\n"
_SUBMIT_CMD = (
    "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\n"
    "diff --git a/x.py b/x.py\\n+    return 0\\n'"
)


def _bug_input():
    return BugInput(bug_id="SWE-1", provider=None, metadata={"swebench_instance": _INSTANCE})


def test_submission_sentinel_yields_fixed(tmp_path):
    model = DeterministicModel(
        outputs=[make_output("Submitting the patch now.", [{"command": _SUBMIT_CMD}])]
    )
    env = LocalEnvironment(cwd=str(tmp_path))
    agent = MiniSweAgent(model=model, env=env, mini_config={"agent": _AGENT_CFG})

    out = agent.fix(_bug_input())

    assert out.outcome == "fixed"
    assert out.bug_id == "SWE-1"
    assert out.error is None
    state = out.final_state or {}
    assert state["swebench_instance_id"] == "astropy__astropy-12345"
    assert state["model_patch"] == _SUBMIT_PATCH
    assert state["mini_exit_status"] == "Submitted"
    assert state["llm_call_count"] == 1


def test_limit_exhausted_yields_no_fix(tmp_path):
    # step_limit=1: one non-submitting action, then the next query trips the
    # limit → mini exits with LimitsExceeded and an empty submission.
    model = DeterministicModel(
        outputs=[make_output("Just looking around.", [{"command": "echo hello"}])]
    )
    env = LocalEnvironment(cwd=str(tmp_path))
    agent = MiniSweAgent(
        model=model, env=env, mini_config={"agent": {**_AGENT_CFG, "step_limit": 1}}
    )

    out = agent.fix(_bug_input())

    assert out.outcome == "no_fix"
    state = out.final_state or {}
    assert state["model_patch"] == ""
    assert state["mini_exit_status"] == "LimitsExceeded"


class _RaisingModel(DeterministicModel):
    """A model that raises on query — mini re-raises truly uncaught errors out
    of run(). (We raise inside query() rather than stashing an exception object
    in the config outputs, which would poison mini's serialize() in its finally
    block and mask the real error.)"""

    def query(self, messages, **kwargs):  # noqa: D401
        raise RuntimeError("kaboom")


def test_uncaught_model_error_yields_error(tmp_path):
    model = _RaisingModel(outputs=[])
    env = LocalEnvironment(cwd=str(tmp_path))
    agent = MiniSweAgent(model=model, env=env, mini_config={"agent": _AGENT_CFG})

    out = agent.fix(_bug_input())

    assert out.outcome == "error"
    assert "kaboom" in (out.error or "")
    # instance id is still recorded on the error path
    assert (out.final_state or {}).get("swebench_instance_id") == "astropy__astropy-12345"


def test_gateway_model_config_points_at_gateway():
    cfg = SimpleNamespace(
        llm_via_gateway=True,
        llm_api_base_url="http://gateway:9000/v1",
        llm_model="qwen3-coder",
        llm_api_key="EMPTY",
    )
    out = build_gateway_model_config(cfg)

    assert out["model_name"] == "openai/qwen3-coder"  # provider prefix added
    assert out["model_kwargs"]["api_base"] == "http://gateway:9000/v1"
    assert out["model_kwargs"]["api_key"] == "EMPTY"


def test_gateway_model_config_worker_overrides_base_model():
    # Worker settings are AUTHORITATIVE for the endpoint: mini's swebench.yaml
    # pins anthropic/claude-sonnet by default, and that MUST NOT leak through
    # (the real bug that made litellm route to Anthropic against DeepSeek's URL).
    cfg = SimpleNamespace(
        llm_via_gateway=False,
        llm_api_base_url="https://api.deepseek.com",
        llm_model="deepseek-v4-pro",
        llm_api_key="EMPTY",
    )
    base = {
        "model_name": "anthropic/claude-sonnet-4-5-20250929",
        "model_kwargs": {"temperature": 0.0, "drop_params": True},
    }
    out = build_gateway_model_config(cfg, base)

    assert out["model_name"] == "openai/deepseek-v4-pro"          # worker wins over base
    assert out["model_kwargs"]["api_base"] == "https://api.deepseek.com"  # worker wins
    # non-endpoint model_kwargs from base are preserved
    assert out["model_kwargs"]["temperature"] == 0.0
    assert out["model_kwargs"]["drop_params"] is True


def test_gateway_model_config_falls_back_to_base_model_when_worker_unset():
    cfg = SimpleNamespace(llm_via_gateway=False, llm_api_base_url="", llm_model="", llm_api_key="")
    base = {"model_name": "deepseek/deepseek-v4-pro", "model_kwargs": {}}
    out = build_gateway_model_config(cfg, base)

    # No worker model/endpoint → base model_name is used verbatim (already prefixed).
    assert out["model_name"] == "deepseek/deepseek-v4-pro"
    assert "api_base" not in out["model_kwargs"]


def test_runrecord_carries_swebench_fields():
    final_state = {
        "swebench_instance_id": "astropy__astropy-12345",
        "resolved": True,
        "model_patch": _SUBMIT_PATCH,
        "total_cost_usd": 0.0012,
        "cost_source": "mini_litellm",
    }
    rec = RunRecord.from_outputs(
        agent_name="mini_swe_agent",
        bug_id="SWE-1",
        outcome="fixed",
        error=None,
        iterations=3,
        final_state=final_state,
    )

    assert rec.swebench_instance_id == "astropy__astropy-12345"
    assert rec.resolved is True
    # cost_source provided by state must be honoured (not overwritten by the
    # gateway-vs-direct inference the LangGraph path uses).
    assert rec.cost_source == "mini_litellm"
    assert rec.cost_currency == "USD"


def test_runrecord_swebench_fields_default_none():
    # A non-SWE-bench run leaves both fields None (never a fabricated False).
    rec = RunRecord.from_outputs(
        agent_name="langgraph",
        bug_id="BUG-1",
        outcome="fixed",
        error=None,
        iterations=0,
        final_state={},
    )
    assert rec.swebench_instance_id is None
    assert rec.resolved is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
