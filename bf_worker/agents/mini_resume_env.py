"""
Resumable docker environment for mini-swe-agent (plan item W2).

Two jobs:

  1. **Re-attach.** mini starts a detached sibling container and hangs cleanup
     off `__del__`, so a SIGKILLed worker leaves it running with every edit and
     install still in place. Given a checkpointed container id, this class
     attaches to it instead of starting a fresh one — that is what makes resume
     worth anything, since the conversation alone would be a pack of lies about
     a container that no longer exists.

  2. **Answer "did the last command run?"** The loop checkpoints at iteration
     boundaries, so a crash *inside* `execute()` leaves an unrecorded command
     that may or may not have taken effect. Every command is therefore prefixed
     with a write of its sequence number to a file inside the container; on
     resume, comparing that marker against the checkpointed sequence turns an
     invisible risk into a decidable, countable one.

**Nothing here edits vendored code.** `_start_container()`, `execute()` and
`cleanup()` are ordinary methods reached through `self`, so subclassing is all
it takes — `vendor/mini/docker_env.py` stays byte-identical to upstream. The
class is selected through mini's own dotted-path `environment_class` seam, the
same mechanism mode 2 uses to swap in its model class.

See /mnt/d/PL/sdlcma/W2-step-checkpoint-design.md §5, §7.2.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess

from agents.vendor.mini.docker_env import DockerEnvironment, DockerEnvironmentConfig
from services.step_checkpoint import StepCheckpointStore, build_step_checkpoint_store

logger = logging.getLogger(__name__)

# tmpfs + dotfile + outside /testbed and /tmp: the point is to minimise the
# chance the agent ever *sees* this file. If it turns up in some `ls -la`, that
# observation text changes, and with it the gateway cache key — which would
# fork the trajectory of every replayed run.
DEFAULT_MARKER_PATH = "/dev/shm/.sdlcma_step"

# reconcile() verdicts
CLEAN = "clean"           # the dead process issued no command we do not know about
AMBIGUOUS = "ambiguous"   # exactly one unrecorded command started; may have completed
MISMATCH = "mismatch"     # marker cannot be explained by the record — do not trust it
UNKNOWN = "unknown"       # no marker available (fresh run, or self-test failed)


def marker_command(seq: int, marker_path: str, command: str) -> str:
    """Wrap a command so the container records that it *started*.

    Four details, each load-bearing (design §7.2):

    - `{ ...; } 2>/dev/null` rather than `printf ... 2>/dev/null`: when the
      redirection itself fails (read-only path), the complaint comes from the
      shell before printf's own stderr redirection is in effect. Since
      `docker exec` here merges stderr into stdout, that text would land in the
      observation and change the cache key. Suppressing the whole group is the
      only version that cannot leak.
    - `;` not `&&`: failing to record the marker must never block the command.
    - bash returns the last command's status, so `returncode` is unchanged.
    - printf writes to a file, so stdout stays empty and mini's submission
      protocol (`_check_finished` inspects the FIRST line of output) is intact.
    """
    return "{ printf '%%s' %d > %s; } 2>/dev/null; %s" % (
        int(seq), shlex.quote(marker_path), command,
    )


class ResumableDockerEnvironmentConfig(DockerEnvironmentConfig):
    sdlcma_resume_key: str = ""
    """Run key to persist the container pointer under. Empty ⇒ no resume."""

    sdlcma_run_fingerprint: str = ""
    """Identity of the run this container belongs to.

    Checked before attaching, and that ordering is the whole point: the batch
    path keys on `instance_id`, so re-running an instance while the previous
    container is still alive is ordinary usage. Catching it here means such a
    run simply gets a clean container, instead of inheriting a /testbed that a
    different run had already edited.
    """


class ResumableDockerEnvironment(DockerEnvironment):
    def __init__(
        self,
        *,
        config_class: type = ResumableDockerEnvironmentConfig,
        store: StepCheckpointStore | None = None,
        **kwargs,
    ):
        # Every attribute _start_container() touches must exist before
        # super().__init__() runs, because it calls that method for us.
        self._store = store if store is not None else build_step_checkpoint_store()
        self._seq = 0
        self._released = False
        self._attached = False
        self._marker_enabled = False
        self._marker_path = os.environ.get("BF_STEP_MARKER_PATH") or DEFAULT_MARKER_PATH
        super().__init__(config_class=config_class, **kwargs)

    # ── state the agent side needs ───────────────────────────────────────────

    @property
    def seq(self) -> int:
        """Commands issued to this container so far (checkpointed as env_seq)."""
        return self._seq

    @property
    def attached(self) -> bool:
        return self._attached

    def _resume_active(self) -> bool:
        # getattr throughout: __del__ can reach here on a half-constructed
        # object (config validation raising leaves `self.config` unset), and an
        # AttributeError there would bury the real error under "Exception
        # ignored in __del__". Upstream's cleanup() guards the same way.
        key = getattr(getattr(self, "config", None), "sdlcma_resume_key", "")
        store = getattr(self, "_store", None)
        return bool(key) and bool(store is not None and store.enabled)

    # ── container lifecycle ──────────────────────────────────────────────────

    def _start_container(self):
        key = self.config.sdlcma_resume_key
        if not self._resume_active():
            super()._start_container()
            return

        rec = self._store.load(key, "env")
        if rec and self._can_attach(rec):
            self.container_id = rec["container_id"]
            self._attached = True
            self._marker_enabled = bool(rec.get("marker_enabled"))
            self._marker_path = rec.get("marker_path") or self._marker_path
            logger.info("resume: attached to container %s (key=%s, marker=%s)",
                        self.container_id, key, self._marker_enabled)
            return

        if rec:
            # ⚠ The container is gone but the conversation is not. Resuming the
            # messages against a fresh container would hand the model a context
            # asserting edits that no longer exist — it would "finish" and
            # submit an empty diff, with nothing anywhere reporting an error.
            # So the agent records die with the container. Always.
            logger.warning("resume: checkpointed container %s is unusable — purging run %s",
                           rec.get("container_id"), key)
            self._store.purge_run(key)

        super()._start_container()
        self._marker_enabled = self._marker_selftest()
        self._store.save(key, "env", {
            "container_id": self.container_id,
            "fingerprint": self.config.sdlcma_run_fingerprint,
            "image": self.config.image,
            "cwd": self.config.cwd,
            "marker_enabled": self._marker_enabled,
            "marker_path": self._marker_path,
        })

    def _can_attach(self, rec: dict) -> bool:
        cid = rec.get("container_id")
        if not cid:
            return False
        if rec.get("fingerprint") != self.config.sdlcma_run_fingerprint:
            logger.warning("resume: checkpointed container %s belongs to a different run "
                           "(fingerprint mismatch) — starting a clean one", cid)
            return False
        if rec.get("image") != self.config.image or rec.get("cwd") != self.config.cwd:
            logger.warning("resume: checkpointed env is for image=%r cwd=%r, we want %r/%r",
                           rec.get("image"), rec.get("cwd"), self.config.image, self.config.cwd)
            return False
        return self._container_running(cid)

    def _container_running(self, container_id: str) -> bool:
        out = self._docker("inspect", "-f", "{{.State.Running}}", container_id)
        return (out or "").strip() == "true"

    def _docker(self, *args: str, timeout: int = 60) -> str | None:
        """Run a docker command OUTSIDE the agent's command stream.

        Everything in here is bookkeeping the agent must never observe, so it
        never goes through execute() and never touches the message history.
        """
        try:
            result = subprocess.run(
                [self.config.executable, *args],
                capture_output=True, text=True, timeout=timeout,
            )
        except Exception as exc:
            logger.warning("resume: docker %s failed: %s", args[0] if args else "?", exc)
            return None
        if result.returncode != 0:
            return None
        return result.stdout

    # ── step marker ──────────────────────────────────────────────────────────

    def _marker_selftest(self) -> bool:
        """Write and read back a sentinel, once, before the loop starts.

        A failure here disables only the *detection*, never the resume. Letting
        a diagnostic facility take down the feature it is diagnosing would be
        the wrong trade every time.
        """
        assert self.container_id
        out = self._docker(
            "exec", self.container_id, *self.config.interpreter,
            marker_command(0, self._marker_path, f"cat {shlex.quote(self._marker_path)}"),
        )
        ok = (out or "").strip() == "0"
        if not ok:
            logger.warning(
                "resume: step-marker self-test failed at %s — resume still works, but a "
                "crash inside a command will not be detectable (design §7.2)",
                self._marker_path,
            )
        return ok

    def _read_marker(self) -> int | None:
        out = self._docker("exec", self.container_id, *self.config.interpreter,
                           f"cat {shlex.quote(self._marker_path)} 2>/dev/null")
        try:
            return int((out or "").strip())
        except ValueError:
            return None

    def reconcile(self, env_seq: int) -> str:
        """Compare the in-container marker with the checkpointed command count.

        Also restores the counter, so marker numbering keeps climbing across
        restarts — otherwise a second crash would reconcile against a sequence
        that had silently restarted at zero.
        """
        self._seq = int(env_seq or 0)
        if not self._attached or not self._marker_enabled:
            return UNKNOWN
        marker = self._read_marker()
        if marker is None:
            # A container we are attached to, that has run commands, must have
            # the marker file. Its absence means this is not the container the
            # record describes.
            return MISMATCH if self._seq > 0 else CLEAN
        if marker <= self._seq:
            return CLEAN
        if marker == self._seq + 1:
            return AMBIGUOUS
        return MISMATCH

    # ── command execution ────────────────────────────────────────────────────

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None):
        # The counter advances even when the marker is off, so `env_seq` stays a
        # faithful command count for the record either way.
        self._seq += 1
        if self._marker_enabled:
            action = {**action, "command": marker_command(
                self._seq, self._marker_path, action.get("command", "") or "")}
        return super().execute(action, cwd, timeout=timeout)

    # ── teardown ─────────────────────────────────────────────────────────────

    def cleanup(self):
        """Keep the container alive while a resume is still possible.

        `__del__` calls this, which is precisely the path we must not let
        destroy the container: an in-process teardown is not the failure mode
        resume exists for. `release()` is the explicit "this run is over" event.
        """
        if getattr(self, "_released", True) or not self._resume_active():
            super().cleanup()

    def release(self):
        """The run is finished in-process — no restart will ever want this.

        Called from MiniSweAgent.fix()'s finally, so the only way to keep a
        container is for the worker to be killed from outside, which is exactly
        the case resume is for.

        Disposes of the container only. Purging the run's records is the
        agent's job (MiniSweAgent.fix), because records outlive any single
        environment — the staged modes share one environment across several
        agents, and an injected environment has no say in the matter at all.
        """
        self._released = True
        super().cleanup()
