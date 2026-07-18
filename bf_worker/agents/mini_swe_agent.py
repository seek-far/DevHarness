"""
MiniSweAgent — wraps mini-swe-agent as an SDLCMA `Agent` (the SWE-bench substrate).

This is the bridge described in `docs/swebench.md`. It drives mini-swe-agent
directly (mini's own Model + Environment + DefaultAgent loop) and maps the
result onto our shared `Agent.fix(BugInput) -> FixOutput` contract.

Deliberate divergence from LangGraphAgent (documented, not an oversight): this
adapter does NOT use the Source/VCS/Review provider trio. SWE-bench source lives
inside the per-instance docker image, not behind a provider; providers are
LangGraphAgent's mechanism, while the `Agent` ABC is the universal seam. The
SWE-bench instance (problem_statement / docker image / instance_id /
FAIL_TO_PASS…) rides on `BugInput.metadata["swebench_instance"]` so the shared
`BugInput` schema stays minimal.

`resolved` is NOT computed here — it is the official SWE-bench harness verdict,
run by the entry point (`bf_worker/swebench_single.py`) AFTER fix() produces the
patch. This adapter only produces the patch (carried in
`FixOutput.final_state["model_patch"]`).

mini-swe-agent is MIT © 2025 Kilian A. Lieret and Carlos E. Jimenez; it is used
as an editable-installed dependency (no source vendored here). See
`THIRD_PARTY_NOTICES.md`.
"""

from __future__ import annotations

import logging
import os

# mini-swe-agent prints a startup banner and touches its global config dir at
# import time unless this is set. Set it before any minisweagent import so
# importing this adapter is side-effect-quiet.
os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

import time
from typing import Any

from agents.base import Agent, BugInput, FixOutput, Outcome

logger = logging.getLogger(__name__)


def swebench_docker_image_name(instance: dict) -> str:
    """Resolve the SWE-bench evaluation docker image for an instance.

    Implements the public SWE-bench image-naming convention (docker doesn't
    allow "__", so it is swapped for the "_1776_" magic token). Prefers an
    explicit `image_name`/`docker_image` on the instance when present.
    """
    name = instance.get("image_name") or instance.get("docker_image")
    if name:
        return name
    iid = instance["instance_id"].replace("__", "_1776_")
    return f"docker.io/swebench/sweb.eval.x86_64.{iid}:latest".lower()


def build_sb_environment(mini_config: dict, instance: dict) -> Any:
    """Build the per-instance mini Environment (docker by default).

    Reimplements mini's get_sb_environment against mini's CORE env layer
    (`minisweagent.environments.get_environment`) rather than importing
    `minisweagent.run.benchmarks.swebench`, whose module-level typer/rich CLI
    imports we don't want to drag into the worker path.
    """
    from minisweagent.environments import get_environment

    env_config = dict(mini_config.get("environment") or {})
    env_config.setdefault("environment_class", "docker")
    image = swebench_docker_image_name(instance)
    ec = env_config["environment_class"]
    if ec in ("docker", "swerex_modal"):
        env_config["image"] = image
    elif ec in ("singularity", "contree"):
        env_config["image"] = "docker://" + image

    env = get_environment(env_config)

    startup = (mini_config.get("run") or {}).get("env_startup_command")
    if startup:
        from jinja2 import StrictUndefined, Template

        rendered = Template(startup, undefined=StrictUndefined).render(**instance)
        out = env.execute({"command": rendered})
        if out.get("returncode") != 0:
            raise RuntimeError(f"env startup command failed: {out}")
    return env


def _litellm_model_name(name: str) -> str:
    """litellm needs a provider prefix. A bare name (no "/") targets a custom
    OpenAI-compatible endpoint, so prefix it with "openai/"; an already-prefixed
    name (e.g. "deepseek/…", "anthropic/…") is left untouched."""
    return f"openai/{name}" if name and "/" not in name else name


def build_gateway_model_config(worker_cfg: Any, base: dict | None = None) -> dict:
    """Build a mini-swe-agent `model` config that targets our LLM gateway or any
    OpenAI-compatible backend from worker settings.

    Worker settings are AUTHORITATIVE for the endpoint — that is the whole point
    of the redirect. `llm_model` / `llm_api_base_url` / `llm_api_key` OVERRIDE
    whatever the base mini config declared (mini's swebench.yaml pins
    `anthropic/claude-sonnet-4-5` by default — without this override that leaks
    through and litellm routes to Anthropic against the wrong URL). All other
    `model_kwargs` from base (temperature, drop_params, parallel_tool_calls, …)
    are preserved. Pure function — unit-tested without a live backend. See
    docs/swebench.md seam 3. (A CLI `--model` still wins over worker settings —
    build_mini_config applies it after this.)
    """
    cfg = dict(base or {})
    model_kwargs = dict(cfg.get("model_kwargs") or {})

    api_base = getattr(worker_cfg, "llm_api_base_url", "") or ""
    api_key = getattr(worker_cfg, "llm_api_key", "") or "EMPTY"
    # worker's llm_model wins; base model_name is only a fallback.
    model = (getattr(worker_cfg, "llm_model", "") or "") or cfg.get("model_name") or ""

    if api_base:
        model_kwargs["api_base"] = api_base
        model_kwargs["api_key"] = api_key

    cfg["model_name"] = _litellm_model_name(model)
    cfg["model_kwargs"] = model_kwargs
    return cfg


class MiniSweAgent(Agent):
    """Run mini-swe-agent against one SWE-bench instance.

    model / env may be injected (unit tests pass mini's DeterministicModel + a
    LocalEnvironment, so CI needs no docker/LLM/network). When not injected they
    are built from `mini_config` — mini's `get_model` and the SWE-bench docker
    `get_sb_environment`.
    """

    name = "mini_swe_agent"

    def __init__(
        self,
        *,
        model: Any = None,
        env: Any = None,
        mini_config: dict | None = None,
        agent_config: dict | None = None,
    ):
        self._model = model
        self._env = env
        self._mini_config = mini_config or {}
        # agent_config is the SDLCMA-side spec (recorded on RunRecord), distinct
        # from mini's own agent config under mini_config["agent"].
        self._agent_config = agent_config or {}

    def fix(self, bug_input: BugInput) -> FixOutput:
        instance = (bug_input.metadata or {}).get("swebench_instance") or {}
        task = (
            instance.get("problem_statement")
            or (bug_input.metadata or {}).get("task")
            or ""
        )

        model = self._model if self._model is not None else self._build_model()
        env = self._env if self._env is not None else self._build_env(instance)
        agent = self._build_mini_agent(model, env)

        t0 = time.monotonic()
        try:
            info = agent.run(task)
        except Exception as exc:  # mini re-raises truly uncaught errors from run()
            elapsed = time.monotonic() - t0
            logger.exception("mini_swe_agent bug=%s crashed", bug_input.bug_id)
            return FixOutput(
                outcome="error",
                bug_id=bug_input.bug_id,
                error=str(exc),
                iterations=int(getattr(agent, "n_calls", 0) or 0),
                final_state=self._final_state(
                    instance, agent, submission="", exit_status=type(exc).__name__, elapsed=elapsed
                ),
            )
        elapsed = time.monotonic() - t0

        exit_status = info.get("exit_status") or ""
        submission = info.get("submission") or ""
        # mini submits by raising Submitted (exit_status="Submitted"); every
        # other terminal status (LimitsExceeded / TimeExceeded / …) means no
        # patch was produced.
        outcome: Outcome = (
            "fixed" if exit_status == "Submitted" and submission.strip() else "no_fix"
        )

        return FixOutput(
            outcome=outcome,
            bug_id=bug_input.bug_id,
            error=None,
            iterations=int(getattr(agent, "n_calls", 0) or 0),
            final_state=self._final_state(instance, agent, submission, exit_status, elapsed),
        )

    # ── internals ────────────────────────────────────────────────────────────

    def _final_state(
        self, instance: dict, agent: Any, submission: str, exit_status: str, elapsed: float
    ) -> dict:
        cost = getattr(agent, "cost", None)
        n_calls = getattr(agent, "n_calls", None)
        state: dict[str, Any] = {
            "swebench_instance_id": instance.get("instance_id"),
            "model_patch": submission,
            "mini_exit_status": exit_status,
            # test_passed / resolved are unknown until the official harness runs
            # in the entry point — left absent so RunRecord keeps them None
            # (never a fabricated False, which would read as "graded, failed").
            "llm_call_count": n_calls,
            "total_llm_wallclock_s": round(elapsed, 3) if elapsed is not None else None,
        }
        # An unknown/zero cost stays None (same invariant as the rest of the
        # system): a $0.00 in a cost dashboard would read as "this was free".
        if cost is not None and cost > 0:
            state["total_cost_usd"] = float(cost)
            state["cost_source"] = "mini_litellm"
        return state

    def _build_model(self) -> Any:
        from minisweagent.models import get_model

        return get_model(config=self._mini_config.get("model", {}))

    def _build_env(self, instance: dict) -> Any:
        # Resolve the per-instance SWE-bench docker image and start the
        # container (see build_sb_environment — mini's CORE env layer, not its
        # CLI module).
        return build_sb_environment(self._mini_config, instance)

    def _build_mini_agent(self, model: Any, env: Any) -> Any:
        from minisweagent.agents.default import DefaultAgent

        return DefaultAgent(model, env, **(self._mini_config.get("agent") or {}))
