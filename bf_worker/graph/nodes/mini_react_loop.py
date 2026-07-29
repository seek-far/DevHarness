"""
Node: mini_react_loop  (workflow_ver == 99 only — the SWE-bench substrate)

Replaces the parse_trace → fetch_source_file → react_loop chain for SWE-bench
instances. Instead of the built-in LangGraph ReAct loop over the provider's
file tools, it runs **mini-swe-agent** inside the per-instance SWE-bench docker
image and captures the unified diff mini produces.

Task source (see docs/swebench.md): everything keys off `state["source_branch"]`
(= the `instance/<instance_id>` branch the failing pipeline ran on, threaded in
from the orchestrator). We read `.swebench/instance.json` from that branch via
`provider.fetch_file(path, ref=source_branch)` — no working-tree checkout, no
new env plumbing, provider-agnostic. That JSON carries the problem_statement +
image + instance_id mini needs.

Output contract:
    model_patch: str | None    the diff mini produced (None ⇒ no patch)
        None → route_after_mini_react_loop → handle_failure
        str  → route_after_mini_react_loop → create_fix_branch → apply
               (apply_change_and_test git-applies it and SKIPS local pytest;
                GitLab CI is the test oracle for this path)

The heavy mini/docker imports live inside `run_mini_for_instance` so importing
this node (which graph/builder.py does at module load) stays cheap and needs no
minisweagent install until a ver==99 run actually executes.
"""

from __future__ import annotations
import json
import logging
from typing import Optional

from langchain_core.runnables import RunnableConfig

from agents.base import BugInput
from graph.state import BugFixState
from services.runtime_context import get_provider

logger = logging.getLogger(__name__)

_INSTANCE_FILE = ".swebench/instance.json"

# Telemetry keys mini's FixOutput.final_state may carry, folded into BugFixState
# so the journal/RunRecord sees LLM cost/token stats the same as the ver==0 path.
_MINI_TELEMETRY_KEYS = (
    "llm_call_count",
    "total_prompt_tokens",
    "total_completion_tokens",
    "total_cached_input_tokens",
    "total_cost_usd",
    "total_llm_wallclock_s",
    "max_input_tokens",
    "cost_source",
    # W2 resume telemetry — present only when BF_STEP_CHECKPOINT was on.
    "step_resume_count",
    "step_resumed_from_step",
    "step_replayed_command_count",
)


def _instance_id_from_branch(source_branch: str) -> str:
    """`instance/sympy__sympy-22914` → `sympy__sympy-22914`. Best-effort fallback
    when instance.json omits instance_id."""
    b = source_branch or ""
    return b.split("instance/", 1)[1] if "instance/" in b else b


def run_mini_for_instance(instance: dict, bug_id: str):
    """Run mini-swe-agent against one SWE-bench instance; return its FixOutput.

    Factored out as a module-level seam so unit tests can monkeypatch it
    without docker / an LLM / minisweagent installed. Live path builds mini's
    SWE-bench config (mini's builtin benchmarks/swebench.yaml), redirects the
    model at the worker's gateway/backend, and runs mini in the per-instance
    docker image.
    """
    from settings import worker_cfg as cfg
    from agents.mini_swe_agent import MiniSweAgent, build_gateway_model_config

    from minisweagent.config import builtin_config_dir, get_config_from_spec

    default_swebench = builtin_config_dir / "benchmarks" / "swebench.yaml"
    mini_config = get_config_from_spec(str(default_swebench))
    # Redirect mini's litellm model at our gateway/OpenAI-compatible backend so
    # the replay cache + cost metering apply (docs/swebench.md seam 3).
    mini_config["model"] = build_gateway_model_config(cfg, dict(mini_config.get("model") or {}))
    # Tag every mini request with the instance id so the gateway can correlate
    # cache hits/misses to THIS instance (mini's litellm calls carry no bug_id
    # otherwise — the gateway logs `bug_id=` empty). The header is NOT part of
    # the cache key (keying is body-only), so this never changes hit/miss
    # behaviour — it only makes the gateway's per-instance miss diagnostics work.
    instance_id = instance.get("instance_id") or bug_id
    mk = mini_config["model"].setdefault("model_kwargs", {})
    mk.setdefault("extra_headers", {})["X-Sdlcma-Bug-Id"] = str(instance_id)

    agent = MiniSweAgent(
        mini_config=mini_config,
        workflow_mode=0,
        agent_config={"llm_model": (mini_config.get("model") or {}).get("model_name")},
    )
    return agent.fix(BugInput(bug_id=bug_id, provider=None,
                              metadata={"swebench_instance": instance}))


def mini_react_loop(state: BugFixState, config: Optional[RunnableConfig] = None) -> BugFixState:
    provider = get_provider(config)
    bug_id = state["bug_id"]
    source_branch = state.get("source_branch") or "main"

    # 1. Read the instance descriptor from the instance branch (no checkout).
    try:
        raw = provider.fetch_file(_INSTANCE_FILE, ref=source_branch)
        instance = json.loads(raw)
    except Exception as exc:
        logger.exception("mini_react_loop: could not read %s from %s",
                         _INSTANCE_FILE, source_branch)
        return {
            "model_patch": None,
            "error": f"swebench instance descriptor unavailable on {source_branch}: {exc}",
        }

    instance_id = instance.get("instance_id") or _instance_id_from_branch(source_branch)
    logger.info("mini_react_loop: bug=%s instance=%s (branch=%s)",
                bug_id, instance_id, source_branch)

    # 2. Run mini in the per-instance docker image (heavy path; monkeypatched in tests).
    try:
        fix_output = run_mini_for_instance(instance, bug_id)
    except Exception as exc:
        logger.exception("mini_react_loop: mini run crashed bug=%s instance=%s", bug_id, instance_id)
        return {
            "swebench_instance_id": instance_id,
            "model_patch": None,
            "error": f"mini run failed for {instance_id}: {exc}",
        }

    fs = fix_output.final_state or {}
    patch = (fs.get("model_patch") or "").strip()

    update: BugFixState = {
        "swebench_instance_id": instance_id,
        "model_patch": patch or None,   # empty → None → handle_failure
        "react_step_count": int(fix_output.iterations or 0),
        "llm_result": {"can_fix": bool(patch)},  # marker so downstream/telemetry sees a decision was made
    }
    for k in _MINI_TELEMETRY_KEYS:
        if k in fs:
            update[k] = fs[k]

    logger.info("mini_react_loop: instance=%s outcome=%s patch=%s calls=%s",
                instance_id, fix_output.outcome,
                f"{len(patch)} chars" if patch else "<empty>",
                fs.get("llm_call_count"))
    return update
