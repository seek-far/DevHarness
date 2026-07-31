"""
Resumable mini-swe-agent loop (plan item W2).

`ResumableAgent` subclasses the vendored `DefaultAgent` and fills in the two
seams that file exposes (`_sdlcma_resume` / `_sdlcma_checkpoint`). Everything
that makes a run resumable lives here rather than in `vendor/`, which is why the
vendored diff is four purely additive lines and its parity with upstream stays
mechanically checkable.

State model — small enough that a full snapshot per step is simpler than deltas
and costs nothing at these sizes:

    messages · n_calls · cost · elapsed · extra_template_vars

`elapsed` is restored by rebasing `_start_time`, for two reasons. `elapsed_seconds`
is a template variable, so a fresh clock could change rendered observation text
on a template that uses it — and observation text is part of the gateway cache
key, so that would fork the trajectory of every replayed run. (Today neither
mini's `swebench.yaml` nor `mini_phases.py` renders it; upstream's
`programbench.yaml` does.) It also keeps `wall_time_limit_seconds` honest, while
excluding downtime — time the worker spent dead is not time the agent spent.

See /mnt/d/PL/sdlcma/W2-step-checkpoint-design.md §4, §5, §7.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from agents.vendor.mini.agent import DefaultAgent
from agents.mini_resume_env import AMBIGUOUS, CLEAN, MISMATCH, UNKNOWN
from services.step_checkpoint import StepCheckpointStore

logger = logging.getLogger(__name__)

_CTX_ATTR = "_sdlcma_ctx"


class StepResumeAborted(RuntimeError):
    """The checkpoint cannot be trusted against this container.

    Raised instead of quietly carrying on, because the alternative is to run in
    a /testbed whose state we cannot account for. The run fails, its records are
    purged, and the next attempt starts clean.
    """


def ambiguous_policy() -> str:
    policy = (os.environ.get("BF_STEP_ON_AMBIGUOUS") or "rerun").strip().lower()
    if policy not in ("rerun", "restart"):
        raise ValueError(
            f"BF_STEP_ON_AMBIGUOUS={policy!r} is not valid (expected 'rerun' or 'restart')"
        )
    return policy


@dataclass
class ResumeCtx:
    """Everything a bound agent needs, kept off the agent's own attributes."""

    store: StepCheckpointStore
    run_key: str
    name: str                       # "agent-0", "agent-1", …
    fingerprint: str
    env: Any = None                 # ResumableDockerEnvironment, when there is one
    step: int = 0
    message_count: int = 0           # len(messages) at the last successful write
    resume_count: int = 0
    resumed_from_step: int | None = None
    replayed_commands: int = 0
    stats: dict = field(default_factory=dict)


def bind_resume(agent, *, store, run_key: str, name: str, fingerprint: str, env=None) -> None:
    """Attach resume state to an agent after construction.

    Deliberately not routed through mini's `config_class`: an extra config field
    would show up in `get_template_vars()` and in the trajectory JSON, and the
    smaller the blast radius of this feature the better.
    """
    setattr(agent, _CTX_ATTR, ResumeCtx(
        store=store, run_key=run_key, name=name, fingerprint=fingerprint, env=env,
    ))


def resume_stats(agent) -> dict:
    """Telemetry for RunRecord. Empty for an unbound agent."""
    ctx: ResumeCtx | None = getattr(agent, _CTX_ATTR, None)
    if ctx is None:
        return {}
    return {
        "step_resume_count": ctx.resume_count,
        "step_resumed_from_step": ctx.resumed_from_step,
        "step_replayed_command_count": ctx.replayed_commands,
    }


class ResumableAgent(DefaultAgent):
    """DefaultAgent + a checkpoint at every loop boundary + resume from one."""

    # ── entry point ──────────────────────────────────────────────────────────

    def run(self, task: str = "", **kwargs) -> dict:
        """Short-circuit a finished record before the loop can take a step.

        `run()`'s loop tests for the exit message only *after* calling `step()`,
        so entering it with an already-terminal history would cost one pointless
        LLM call. Handling it here — rather than reshaping the vendored loop —
        is what keeps that file's diff purely additive.

        Reached when the mini run completed but the worker died later (during
        git apply / push / CI wait), and in the staged modes for phases that had
        already finished before the crash.
        """
        ctx: ResumeCtx | None = getattr(self, _CTX_ATTR, None)
        if ctx is not None:
            rec = self._load_record(ctx)
            if rec is not None and rec.get("status") == "done":
                self.extra_template_vars |= {"task": task, **kwargs}
                self._restore(ctx, rec)
                logger.info("resume: %s/%s already finished (%d calls) — replaying its result",
                            ctx.run_key, ctx.name, self.n_calls)
                self.save(self.config.output_path)
                return self.messages[-1].get("extra", {}) if self.messages else {}
        return super().run(task, **kwargs)

    # ── seams ────────────────────────────────────────────────────────────────

    def _sdlcma_resume(self) -> None:
        ctx: ResumeCtx | None = getattr(self, _CTX_ATTR, None)
        if ctx is None:
            return
        try:
            rec = self._load_record(ctx)
            if rec is None or rec.get("status") == "done":
                return
            # Restore BEFORE reconciling: _restore loads the counters from the
            # record, so reconciling first would have its increment of
            # replayed_commands immediately overwritten by the stored value.
            self._restore(ctx, rec)
            self._reconcile(ctx, int(rec.get("env_seq") or 0))
            ctx.resumed_from_step = ctx.step
            logger.info("resume: %s/%s continuing from step %s (%d calls, %d messages)",
                        ctx.run_key, ctx.name, ctx.step, self.n_calls, len(self.messages))
        except StepResumeAborted:
            raise
        except Exception:
            # Runtime-forgiving: a bad read costs a re-run, never the run itself.
            logger.warning("resume: could not restore %s/%s — starting fresh",
                           ctx.run_key, ctx.name, exc_info=True)

    def _sdlcma_checkpoint(self) -> None:
        ctx: ResumeCtx | None = getattr(self, _CTX_ATTR, None)
        if ctx is None:
            return
        try:
            if not self.messages:
                return
            if len(self.messages) == ctx.message_count:
                # Nothing was added since the last checkpoint, so the iteration
                # died before the model answered (a clean boundary kill). The
                # record already describes this exact state; rewriting it would
                # only inflate `step` past the number of iterations that ran.
                return
            if self.messages[-1].get("role") == "assistant":
                # Half an iteration: the model answered but its commands have no
                # observations yet, so `execute()` was interrupted. Persisting
                # this would resume into a conversation whose last turn is an
                # unanswered assistant message, and the next query would stack a
                # second assistant turn on top of it. Leave the previous
                # boundary as the record — reconcile() is what then decides
                # whether that command had already started.
                #
                # Reachable in production whenever a BaseException escapes
                # step(): SystemExit from a SIGTERM handler, KeyboardInterrupt,
                # a budget abort. (An ordinary Exception cannot get here in this
                # state — the loop's `except` appends an exit message first.)
                logger.debug("resume: skipping checkpoint mid-iteration for %s/%s",
                             ctx.run_key, ctx.name)
                return
            ctx.step += 1
            done = self.messages[-1].get("role") == "exit"
            ctx.store.save(ctx.run_key, ctx.name, {
                "fingerprint": ctx.fingerprint,
                "status": "done" if done else "running",
                "step": ctx.step,
                "env_seq": int(getattr(ctx.env, "seq", 0) or 0),
                "n_calls": self.n_calls,
                "cost": self.cost,
                "elapsed_s": time.time() - self._start_time,
                "resume_count": ctx.resume_count,
                "replayed_command_count": ctx.replayed_commands,
                "extra_template_vars": self.extra_template_vars,
                "messages": self.messages,
            })
            ctx.message_count = len(self.messages)
        except Exception:
            # This runs inside the loop's `finally`. Letting anything escape
            # would replace the agent's real exception with a storage one.
            logger.warning("resume: checkpoint write failed for %s/%s",
                           ctx.run_key, ctx.name, exc_info=True)

    # ── internals ────────────────────────────────────────────────────────────

    def _load_record(self, ctx: ResumeCtx) -> dict | None:
        rec = ctx.store.load(ctx.run_key, ctx.name)
        if rec is None:
            return None
        if rec.get("fingerprint") != ctx.fingerprint:
            # Normally unreachable: the environment validates the same identity
            # before attaching and purges on mismatch, so by the time we get
            # here the container is known to belong to this run.
            logger.warning("resume: record %s/%s belongs to a different run — ignoring",
                           ctx.run_key, ctx.name)
            return None
        return rec

    def _restore(self, ctx: ResumeCtx, rec: dict) -> None:
        self.messages = list(rec.get("messages") or [])
        self.n_calls = int(rec.get("n_calls") or 0)
        self.cost = float(rec.get("cost") or 0.0)
        self._start_time = time.time() - float(rec.get("elapsed_s") or 0.0)
        self.extra_template_vars |= dict(rec.get("extra_template_vars") or {})
        ctx.step = int(rec.get("step") or 0)
        ctx.message_count = len(self.messages)
        ctx.resume_count = int(rec.get("resume_count") or 0) + 1
        ctx.replayed_commands = int(rec.get("replayed_command_count") or 0)

    def _reconcile(self, ctx: ResumeCtx, env_seq: int) -> None:
        """Decide what the container says about the un-checkpointed command."""
        env = ctx.env
        if env is None or not hasattr(env, "reconcile"):
            return
        verdict = env.reconcile(env_seq)

        if verdict in (CLEAN, UNKNOWN):
            return

        if verdict == AMBIGUOUS:
            # The commands from the iteration that never got checkpointed had
            # started. Whether they finished is unknowable — but they are now
            # *counted*, which is the difference between a measured risk and a
            # story about one. One iteration can issue several commands (mini
            # executes every action in a message), so the count is the marker
            # gap, not a hard-coded 1.
            pending = max(1, int(getattr(env, "pending_commands", 0) or 0))
            last = env_seq + pending
            if ambiguous_policy() == "restart":
                logger.warning("resume: %s/%s — command(s) #%d..#%d may have run; "
                               "BF_STEP_ON_AMBIGUOUS=restart, so discarding the run",
                               ctx.run_key, ctx.name, env_seq + 1, last)
                ctx.store.purge_run(ctx.run_key)
                raise StepResumeAborted(
                    f"command(s) #{env_seq + 1}..#{last} may already have executed; "
                    f"restarting per BF_STEP_ON_AMBIGUOUS=restart"
                )
            ctx.replayed_commands += pending
            logger.warning("resume: %s/%s — command(s) #%d..#%d may already have executed; "
                           "re-issuing them (at-least-once, see design §7.2)",
                           ctx.run_key, ctx.name, env_seq + 1, last)
            return

        if verdict == MISMATCH:
            logger.error("resume: %s/%s — in-container marker cannot be explained by the "
                         "record (env_seq=%d); the container is not the one this record "
                         "describes. Purging so the next attempt starts clean.",
                         ctx.run_key, ctx.name, env_seq)
            ctx.store.purge_run(ctx.run_key)
            raise StepResumeAborted(
                f"step marker disagrees with checkpoint (env_seq={env_seq}) for {ctx.run_key}"
            )
