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
from pathlib import Path
from typing import Any

from agents.base import Agent, BugInput, FixOutput, Outcome

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKGROUND_KNOWLEDGE_BY_INSTANCE = {
    "django__django-15098": _REPO_ROOT / "trial" / "BCP47.md",
}


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
    # Determinism env applied to EVERY command mini runs in the container. This
    # removes the two non-normalizable sources of tool-output drift found by the
    # cache-miss diagnostics (docs/swebench.md): PYTHONUNBUFFERED makes stdout
    # unbuffered so it interleaves with stderr in a stable order (kills the
    # "traceback appears before/after the print" divergence), and PYTHONHASHSEED
    # pins set/dict iteration + unittest test-discovery order (kills the
    # "different test ran at this position" divergence). Both are standard
    # reproducibility settings; they only affect what the AGENT observes while
    # solving — the official harness grades the final patch in its own env — but
    # they DO change trajectories, so a cache recorded with them only replays
    # against runs that also set them. Set on a COPY so we never mutate the
    # shared builtin config dict.
    env_env = dict(env_config.get("env") or {})
    env_env.setdefault("PYTHONUNBUFFERED", "1")
    env_env.setdefault("PYTHONHASHSEED", "0")
    env_config["env"] = env_env
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


def _outcome_from(exit_status: str, submission: str) -> Outcome:
    # mini submits by raising Submitted (exit_status="Submitted"); every other
    # terminal status (LimitsExceeded / TimeExceeded / …) means no patch.
    return "fixed" if exit_status == "Submitted" and submission.strip() else "no_fix"


def _calls(agents: list) -> int:
    return sum(int(getattr(a, "n_calls", 0) or 0) for a in agents)


def _token_stats(agents: list) -> dict:
    """Aggregate per-call token usage across the agents' messages. mini embeds
    the full litellm response (incl. `usage`) in each assistant message's
    `extra.response`, so we read it from memory — no trajectory file needed.

    Returns {} when no usage was reported (e.g. the DeterministicModel used in
    tests) so RunRecord's token fields stay None, never a fabricated 0.
    `max_input_tokens` is the peak prompt size — the metric that tells whether a
    phased run actually ran on shorter context than the single loop.
    """
    prompt = compl = cached = 0
    max_input = 0
    saw_usage = saw_cached = False
    for a in agents:
        for m in getattr(a, "messages", []) or []:
            resp = (m.get("extra") or {}).get("response")
            usage = resp.get("usage") if isinstance(resp, dict) else None
            if not isinstance(usage, dict):
                continue
            saw_usage = True
            pt = int(usage.get("prompt_tokens") or 0)
            prompt += pt
            compl += int(usage.get("completion_tokens") or 0)
            max_input = max(max_input, pt)
            details = usage.get("prompt_tokens_details")
            if isinstance(details, dict) and details.get("cached_tokens") is not None:
                saw_cached = True
                cached += int(details.get("cached_tokens") or 0)
    if not saw_usage:
        return {}
    return {
        "total_prompt_tokens": prompt,
        "total_completion_tokens": compl,
        "max_input_tokens": max_input,
        # None (not 0) when the backend never reported prompt caching.
        "total_cached_input_tokens": cached if saw_cached else None,
    }


def _stage_sequence(agent: Any) -> list:
    """The ordered stages the LLM reported (mode 2), read from the STRUCTURED
    `stage` argument the StageReportingModel captures onto each action (see
    agents/mini_stage_model.py) — reliable for reasoning models whose text
    `content` is empty. One stage per assistant turn (its first action)."""
    seq = []
    for m in getattr(agent, "messages", []) or []:
        if m.get("role") != "assistant":
            continue
        for a in (m.get("extra") or {}).get("actions") or []:
            st = a.get("stage")
            if st is not None:
                seq.append(int(st))
                break
    return seq


def _stage_back_edges(seq: list) -> int:
    """A back-edge = a turn reporting an EARLIER stage than the previous turn."""
    return sum(1 for i in range(1, len(seq)) if seq[i] < seq[i - 1])


def _task_with_background_knowledge(instance: dict, task: str) -> tuple[str, dict]:
    """mode 4: append selected general background knowledge after the PR description.

    Selection is deliberately narrow while the trial only covers django__django-15098.
    Missing files do not break the run; the mode simply falls back to mode 0's
    prompt shape and records that nothing was injected.
    """
    iid = instance.get("instance_id") or ""
    path = _BACKGROUND_KNOWLEDGE_BY_INSTANCE.get(iid)
    if path is None:
        return task, {"background_knowledge_injected": False}
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("background knowledge unavailable for %s at %s: %s", iid, path, exc)
        return task, {
            "background_knowledge_injected": False,
            "background_knowledge_path": str(path),
            "background_knowledge_error": str(exc),
        }
    if not text:
        return task, {
            "background_knowledge_injected": False,
            "background_knowledge_path": str(path),
            "background_knowledge_error": "empty file",
        }
    injected = (
        f"{task}\n\n"
        "## Background knowledge\n\n"
        "The following non-code background reference may be useful. Treat it as "
        "context, not as a patch or repository-specific fact.\n\n"
        "If this background describes a published standard or specification, use "
        "the standard's documented structure as a constraint when designing the "
        "fix. Prefer project-consistent parsing or validation that follows the "
        "standard's subtag/field shapes over a broad pattern that merely accepts "
        "more separators or arbitrary word chunks. Do not hard-code the examples; "
        "generalize from the structural rules in the reference.\n\n"
        f"{text}\n"
    )
    return injected, {
        "background_knowledge_injected": True,
        "background_knowledge_path": str(path),
        "background_knowledge_chars": len(text),
    }


def _phase_extra(agent1: Any, agent2: Any, handoff: str) -> dict:
    return {
        "phase1_handoff": handoff,
        "phase1_calls": int(getattr(agent1, "n_calls", 0) or 0),
        "phase2_calls": int(getattr(agent2, "n_calls", 0) or 0),
    }


def _v3_extra(handoff: str, back_edges: int) -> dict:
    """mode 3 telemetry: how many Investigate⇄Solve back-edges were taken, plus
    the final handoff (Investigate rounds = back_edges + 1)."""
    return {"phase1_handoff": handoff, "v3_back_edges": back_edges}


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
        workflow_mode: int = 0,
        trajectory_dir: str | Path | None = None,
    ):
        self._model = model
        self._env = env
        self._mini_config = mini_config or {}
        # When set, each phase/run writes its full mini trajectory (messages +
        # per-call litellm usage) here — for failure-mode diffing and token
        # analysis. None ⇒ don't save to disk (token stats still land on the
        # RunRecord, read from the in-memory messages).
        self._trajectory_dir = Path(trajectory_dir) if trajectory_dir else None
        # agent_config is the SDLCMA-side spec (recorded on RunRecord), distinct
        # from mini's own agent config under mini_config["agent"].
        self._agent_config = agent_config or {}
        # workflow_mode selects the workflow (see agents/mini_phases.py + the
        # mode table in docs/swebench.md):
        #   0 = mini default single ReAct loop (unchanged behaviour)
        #   1 = two-phase waterfall (Investigate → Solve)
        #   2 = mode 0 + LLM self-reports its workflow stage (1-5) each turn
        #   3 = mode 1 + bounded Investigate⇄Solve back-edge
        #   4 = mode 0 + selected general background knowledge after task
        # Old numbers stay stable so eval comparisons hold across time.
        self._workflow_mode = int(workflow_mode or 0)
        # Encode the mode in the agent name so eval reports (grouped by
        # agent_name) place modes on separate rows for side-by-side comparison.
        # Mode 0 keeps the plain name so existing journals/records don't shift.
        self.name = (
            "mini_swe_agent" if self._workflow_mode == 0
            else f"mini_swe_agent_wf{self._workflow_mode}"
        )

    def fix(self, bug_input: BugInput) -> FixOutput:
        instance = (bug_input.metadata or {}).get("swebench_instance") or {}
        task = (
            instance.get("problem_statement")
            or (bug_input.metadata or {}).get("task")
            or ""
        )
        extra: dict | None = None
        if self._workflow_mode == 4:
            task, extra = _task_with_background_knowledge(instance, task)

        # Build model + environment ONCE. In phased mode both phases share this
        # environment, so the docker container persists across phases — the repro
        # script and edits carry forward even though the conversation is dropped.
        model = self._model if self._model is not None else self._build_model()
        env = self._env if self._env is not None else self._build_env(instance)

        if self._workflow_mode == 1:
            return self._run_phased(bug_input, instance, task, model, env)
        if self._workflow_mode == 3:
            return self._run_phased_v3(bug_input, instance, task, model, env)
        # mode 0 (default), mode 2, and mode 4 share the single-loop path.
        # mode 2 adds stage reporting; mode 4 has already appended background
        # knowledge to `task` when configured for this instance.
        return self._run_single(bug_input, instance, task, model, env,
                                stage_report=(self._workflow_mode == 2),
                                extra=extra)

    # ── mode 0 / mode 2 / mode 4: single ReAct loop ───────────────────────────

    def _traj_path(self, instance: dict, suffix: str = "") -> Path | None:
        if not self._trajectory_dir:
            return None
        iid = instance.get("instance_id") or "unknown"
        name = f"{iid}.{suffix}.json" if suffix else f"{iid}.traj.json"
        return self._trajectory_dir / name

    def _stage_extra(self, agent: Any, stage_report: bool) -> dict | None:
        """mode 2: the reported stage sequence + back-edge count for final_state."""
        if not stage_report:
            return None
        seq = _stage_sequence(agent)
        return {"stage_sequence": seq, "stage_back_edges": _stage_back_edges(seq)}

    def _run_single(self, bug_input, instance, task, model, env,
                    stage_report=False, extra: dict | None = None) -> FixOutput:
        suffix = ""
        if stage_report:
            from agents.mini_phases import STAGE_REPORT_SUFFIX
            suffix = STAGE_REPORT_SUFFIX
        agent = self._build_mini_agent(model, env, output_path=self._traj_path(instance),
                                       instance_template_suffix=suffix)
        t0 = time.monotonic()
        try:
            info = agent.run(task)
        except Exception as exc:  # mini re-raises truly uncaught errors from run()
            elapsed = time.monotonic() - t0
            logger.exception("mini_swe_agent bug=%s crashed", bug_input.bug_id)
            return FixOutput(
                outcome="error", bug_id=bug_input.bug_id, error=str(exc),
                iterations=int(getattr(agent, "n_calls", 0) or 0),
                final_state=self._build_final_state(
                    instance, agents=[agent], submission="",
                    exit_status=type(exc).__name__, elapsed=elapsed,
                    extra={**(extra or {}), **(self._stage_extra(agent, stage_report) or {})}),
            )
        elapsed = time.monotonic() - t0
        exit_status = info.get("exit_status") or ""
        submission = info.get("submission") or ""
        outcome: Outcome = _outcome_from(exit_status, submission)
        return FixOutput(
            outcome=outcome, bug_id=bug_input.bug_id, error=None,
            iterations=int(getattr(agent, "n_calls", 0) or 0),
            final_state=self._build_final_state(
                instance, agents=[agent], submission=submission,
                exit_status=exit_status, elapsed=elapsed,
                extra={**(extra or {}), **(self._stage_extra(agent, stage_report) or {})}),
        )

    # ── mode 1: two-phase waterfall (Investigate → Solve) ─────────────────────

    def _run_phased(self, bug_input, instance, task, model, env) -> FixOutput:
        from minisweagent.agents.default import DefaultAgent
        from agents.mini_phases import (
            INVESTIGATE_INSTANCE_TEMPLATE, INVESTIGATE_STEP_LIMIT, INVESTIGATE_SYSTEM_TEMPLATE,
            SOLVE_INSTANCE_TEMPLATE, SOLVE_STEP_LIMIT, SOLVE_SYSTEM_TEMPLATE,
            phase_agent_config,
        )

        base_agent = self._mini_config.get("agent") or {}
        t0 = time.monotonic()

        # Phase 1 — Investigate (analyze + reproduce; submits a handoff, not a fix).
        p1_cfg = phase_agent_config(
            base_agent, system_template=INVESTIGATE_SYSTEM_TEMPLATE,
            instance_template=INVESTIGATE_INSTANCE_TEMPLATE, step_limit=INVESTIGATE_STEP_LIMIT,
            output_path=self._traj_path(instance, "phase1"))
        agent1 = DefaultAgent(model, env, **p1_cfg)
        try:
            info1 = agent1.run(task)
        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.exception("mini_swe_agent bug=%s crashed in phase 1", bug_input.bug_id)
            return FixOutput(
                outcome="error", bug_id=bug_input.bug_id, error=f"phase1: {exc}",
                iterations=int(getattr(agent1, "n_calls", 0) or 0),
                final_state=self._build_final_state(
                    instance, agents=[agent1], submission="",
                    exit_status=type(exc).__name__, elapsed=elapsed,
                    extra={"phase1_handoff": "", "phase1_calls": int(getattr(agent1, "n_calls", 0) or 0)}),
            )
        # Phase 1 submits the handoff via the normal sentinel; a limit-exhausted
        # phase 1 yields "" — phase 2 still runs (it can re-read files itself).
        handoff = info1.get("submission") or ""

        # Phase 2 — Solve (edit + verify + harden). SAME env → container persists.
        p2_cfg = phase_agent_config(
            base_agent, system_template=SOLVE_SYSTEM_TEMPLATE,
            instance_template=SOLVE_INSTANCE_TEMPLATE, step_limit=SOLVE_STEP_LIMIT,
            output_path=self._traj_path(instance, "phase2"))
        agent2 = DefaultAgent(model, env, **p2_cfg)
        try:
            info2 = agent2.run(task, handoff=handoff)
        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.exception("mini_swe_agent bug=%s crashed in phase 2", bug_input.bug_id)
            return FixOutput(
                outcome="error", bug_id=bug_input.bug_id, error=f"phase2: {exc}",
                iterations=_calls([agent1, agent2]),
                final_state=self._build_final_state(
                    instance, agents=[agent1, agent2], submission="",
                    exit_status=type(exc).__name__, elapsed=elapsed,
                    extra=_phase_extra(agent1, agent2, handoff)),
            )
        elapsed = time.monotonic() - t0
        exit_status = info2.get("exit_status") or ""
        submission = info2.get("submission") or ""
        outcome = _outcome_from(exit_status, submission)
        return FixOutput(
            outcome=outcome, bug_id=bug_input.bug_id, error=None,
            iterations=_calls([agent1, agent2]),
            final_state=self._build_final_state(
                instance, agents=[agent1, agent2], submission=submission,
                exit_status=exit_status, elapsed=elapsed,
                extra=_phase_extra(agent1, agent2, handoff)),
        )

    # ── mode 3: two-phase with bounded back-edge (Investigate ⇄ Solve) ────────

    def _run_phased_v3(self, bug_input, instance, task, model, env) -> FixOutput:
        from minisweagent.agents.default import DefaultAgent
        from agents.mini_phases import (
            INVESTIGATE_INSTANCE_TEMPLATE_V3, INVESTIGATE_STEP_LIMIT, INVESTIGATE_SYSTEM_TEMPLATE,
            MAX_BACK_EDGES, REINVEST_MARKER, SOLVE_INSTANCE_TEMPLATE_V3, SOLVE_STEP_LIMIT,
            SOLVE_SYSTEM_TEMPLATE, phase_agent_config,
        )

        base_agent = self._mini_config.get("agent") or {}
        t0 = time.monotonic()
        agents: list = []
        handoff = reinvest = prior_handoff = ""
        back_edges = 0
        rnd = 0

        def _err(where: str, exc: Exception) -> FixOutput:
            logger.exception("mini_swe_agent bug=%s crashed in %s", bug_input.bug_id, where)
            return FixOutput(
                outcome="error", bug_id=bug_input.bug_id, error=f"{where}: {exc}",
                iterations=_calls(agents),
                final_state=self._build_final_state(
                    instance, agents=agents, submission="", exit_status=type(exc).__name__,
                    elapsed=time.monotonic() - t0, extra=_v3_extra(handoff, back_edges)),
            )

        while True:
            # ── Investigate (round rnd) — seeded with the Solve request on re-rounds
            i_cfg = phase_agent_config(
                base_agent, system_template=INVESTIGATE_SYSTEM_TEMPLATE,
                instance_template=INVESTIGATE_INSTANCE_TEMPLATE_V3, step_limit=INVESTIGATE_STEP_LIMIT,
                output_path=self._traj_path(instance, f"investigate{rnd}"))
            agent_i = DefaultAgent(model, env, **i_cfg)
            agents.append(agent_i)
            try:
                info_i = agent_i.run(task, reinvest_request=reinvest, prior_handoff=prior_handoff)
            except Exception as exc:
                return _err(f"investigate{rnd}", exc)
            handoff = info_i.get("submission") or ""

            # ── Solve (round rnd) — may submit a patch OR request re-investigation
            can_reinvestigate = back_edges < MAX_BACK_EDGES
            s_cfg = phase_agent_config(
                base_agent, system_template=SOLVE_SYSTEM_TEMPLATE,
                instance_template=SOLVE_INSTANCE_TEMPLATE_V3, step_limit=SOLVE_STEP_LIMIT,
                output_path=self._traj_path(instance, f"solve{rnd}"))
            agent_s = DefaultAgent(model, env, **s_cfg)
            agents.append(agent_s)
            try:
                info_s = agent_s.run(task, handoff=handoff, can_reinvestigate=can_reinvestigate,
                                     back_edges_left=MAX_BACK_EDGES - back_edges)
            except Exception as exc:
                return _err(f"solve{rnd}", exc)
            exit_status = info_s.get("exit_status") or ""
            submission = info_s.get("submission") or ""

            # A back-edge = Solve submitted a re-investigation request (marker) and
            # rounds remain. Otherwise the submission is the final patch (or no_fix).
            is_reinvest = (
                can_reinvestigate and exit_status == "Submitted"
                and submission.lstrip().startswith(REINVEST_MARKER)
            )
            if not is_reinvest:
                return FixOutput(
                    outcome=_outcome_from(exit_status, submission), bug_id=bug_input.bug_id, error=None,
                    iterations=_calls(agents),
                    final_state=self._build_final_state(
                        instance, agents=agents, submission=submission, exit_status=exit_status,
                        elapsed=time.monotonic() - t0, extra=_v3_extra(handoff, back_edges)),
                )
            # back-edge → seed the next Investigate round with Solve's request
            reinvest, prior_handoff = submission, handoff
            back_edges += 1
            rnd += 1

    # ── internals ────────────────────────────────────────────────────────────

    def _build_final_state(
        self, instance: dict, *, agents: list, submission: str, exit_status: str,
        elapsed: float, extra: dict | None = None,
    ) -> dict:
        total_calls = _calls(agents)
        total_cost = sum(float(getattr(a, "cost", 0) or 0) for a in agents)
        state: dict[str, Any] = {
            "swebench_instance_id": instance.get("instance_id"),
            "model_patch": submission,
            "mini_exit_status": exit_status,
            # test_passed / resolved stay unset until the official harness runs in
            # the entry point (None ≠ a fabricated False).
            "llm_call_count": total_calls,
            "total_llm_wallclock_s": round(elapsed, 3) if elapsed is not None else None,
            "workflow_mode": self._workflow_mode,
        }
        # Per-call token usage (total_prompt/completion/max_input/cached) — read
        # from the agents' messages; empty (fields stay None) when the backend
        # reported no usage.
        state.update(_token_stats(agents))
        # An unknown/zero cost stays None (a $0.00 would read as "this was free").
        if total_cost > 0:
            state["total_cost_usd"] = total_cost
            state["cost_source"] = "mini_litellm"
        if extra:
            state.update(extra)
        return state

    def _build_model(self) -> Any:
        from minisweagent.models import get_model

        model_cfg = dict(self._mini_config.get("model", {}))
        # mode 2: swap in the stage-reporting model (adds a required `stage`
        # arg to the bash tool) via get_model's model_class override.
        if self._workflow_mode == 2:
            model_cfg["model_class"] = "agents.mini_stage_model.StageReportingModel"
        return get_model(config=model_cfg)

    def _build_env(self, instance: dict) -> Any:
        # Resolve the per-instance SWE-bench docker image and start the
        # container (see build_sb_environment — mini's CORE env layer, not its
        # CLI module).
        return build_sb_environment(self._mini_config, instance)

    def _build_mini_agent(self, model: Any, env: Any, output_path: Path | None = None,
                          instance_template_suffix: str = "") -> Any:
        from minisweagent.agents.default import DefaultAgent

        cfg = dict(self._mini_config.get("agent") or {})
        if output_path is not None:
            cfg["output_path"] = output_path
        if instance_template_suffix:
            cfg["instance_template"] = (cfg.get("instance_template") or "") + instance_template_suffix
        return DefaultAgent(model, env, **cfg)
