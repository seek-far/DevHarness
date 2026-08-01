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

import hashlib
import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass

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

# ── command ledger (W2.5) ────────────────────────────────────────────────────
#
# Not /tmp: the agent runs `ls /tmp` and the directory would land in an
# observation, which changes the gateway cache key. Not /dev/shm either (where
# W2's few-byte marker lives): that is a tmpfs capped at docker's default
# --shm-size=64m, and a command's captured output can be megabytes — filling it
# would truncate output silently, which is far worse than not resuming.
DEFAULT_LEDGER_DIR = "/.sdlcma"

# Bookkeeping shells are plain `sh -c`, NOT config.interpreter (`bash -lc`).
# A login shell sources /etc/profile; anything it prints would be prepended to
# the observation by our waiter — text upstream never produces. The agent's own
# command still runs under config.interpreter, exactly as upstream runs it.
_BOOKKEEPING = ("sh", "-c")

# The in-container waiter's own bail-out code. Deliberately NOT trusted as a
# discriminator (a real command can exit 99 too) — it is a hint that triggers
# one probe, and the presence of the `rc` file is what actually decides.
_WAITER_TIMEOUT_RC = 99
_WAITER_SLACK_S = 5     # host-side backstop beyond the in-container timeout


class LedgerMismatch(RuntimeError):
    """The container's ledger cannot be reconciled with our record.

    Raised instead of executing anything: a `<seq>` directory holding a
    different command means the sequence numbering and the container have come
    apart, and every subsequent decision would be made against the wrong
    evidence.
    """


@dataclass(frozen=True)
class Probe:
    """Everything §4.2's eight-row matrix needs, from one `docker exec`."""

    present: bool = False           # the <seq> directory exists
    cmd_sha256: str = ""            # identity of the command recorded there
    started: float | None = None    # None ⇒ never launched (crashed between execs)
    rc: int | None = None           # None ⇒ not finished
    out_size: int = 0               # diagnostics only


def ledger_mode() -> str:
    """`ledger` (default) or `marker` (W2 semantics, kept as the A/B arm)."""
    mode = (os.environ.get("BF_STEP_LEDGER") or "ledger").strip().lower()
    if mode not in ("ledger", "marker"):
        raise ValueError(
            f"BF_STEP_LEDGER={mode!r} is not valid (expected 'ledger' or 'marker')"
        )
    return mode


def ledger_dir() -> str:
    return os.environ.get("BF_STEP_LEDGER_DIR") or DEFAULT_LEDGER_DIR


def probe_script(d: str) -> str:
    """One-shot state read. Output is fixed-size regardless of command size.

    Identity travels as a hash, not as the command text: mini writes files with
    heredocs, so a command body of several KB is ordinary, and shipping it back
    through `docker exec` on every probe would be both wasteful and a decoding
    hazard (this class reads docker's stdout as text). `sha256sum` missing from
    an image degrades to an empty hash, which the caller answers by falling
    back to a raw comparison rather than by refusing to run.
    """
    q = shlex.quote(d)
    return (
        f"cd {q} 2>/dev/null || {{ echo ABSENT; exit 0; }}; "
        f"printf 'CMD %s\\n' \"$(sha256sum cmd 2>/dev/null | cut -d' ' -f1)\"; "
        f"if [ -f started ]; then printf 'STARTED %s\\n' \"$(cat started)\"; fi; "
        f"if [ -f rc ]; then printf 'RC %s\\n' \"$(cat rc)\"; fi; "
        f"printf 'SIZE %s\\n' \"$(wc -c < out 2>/dev/null || echo 0)\"; "
        f"exit 0"
    )


def parse_probe(text: str | None) -> Probe:
    if not text or "ABSENT" in text.split("\n")[0]:
        return Probe(present=False)
    cmd_sha, started, rc, size = "", None, None, 0
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        value = value.strip()
        try:
            if key == "CMD":
                cmd_sha = value
            elif key == "STARTED":
                started = float(value)
            elif key == "RC":
                rc = int(value)
            elif key == "SIZE":
                size = int(value)
        except ValueError:
            # A half-written field is treated as absent. Both files that matter
            # are written via same-directory rename, so this should be
            # unreachable; being lenient here costs one re-issue at worst.
            continue
    return Probe(present=True, cmd_sha256=cmd_sha, started=started, rc=rc, out_size=size)


def write_cmd_script(d: str) -> str:
    """Clear the slot and take the command body from stdin.

    `rm -rf` first because this is also the re-issue path for a command that was
    recorded but never launched (§4.2 row 4); leftovers there would be read as
    evidence about a command that never ran.

    The body travels through stdin rather than through the script text so that
    it never passes a shell quoting layer — with two nested shells any command
    containing quotes, `$`, or newlines would eventually break.
    """
    q = shlex.quote(d)
    return f"rm -rf {q} && mkdir -p {q} && cat > {q}/cmd"


def launch_script(d: str, seq: int, marker_path: str, interpreter: list[str],
                  marker_enabled: bool) -> str:
    """Record the start time, then detach the command from this exec client.

    Three things are load-bearing:

    - `rc` is written by the detached session ITSELF, not by a parent that
      waits. A parent dies with the `docker exec` client when the worker is
      killed, which would leave a finished command looking like a running one
      forever.
    - `started` is written BEFORE `setsid`, so the ambiguous window is "claims
      to have started but did not" rather than the reverse. Waiting one extra
      timeout for a command that never ran is recoverable; re-running a command
      that did run is exactly the thing this feature exists to prevent.
    - the command body is read with `"$(cat …)"` inside the detached shell, so
      the quoting depth stays at zero no matter what the model wrote.
    """
    q = shlex.quote(d)
    inner = (
        f"{shlex.join(interpreter)} \"$(cat {q}/cmd)\" > {q}/out 2>&1; "
        f"echo $? > {q}/rc.t; mv {q}/rc.t {q}/rc"
    )
    marker = ""
    if marker_enabled:
        # W2's marker keeps being written: it costs nothing, keeps the two
        # modes' container state a superset of each other, and stays as
        # defence-in-depth for the one case the ledger cannot see (the pending
        # record failing to persist at all).
        marker = f"{{ printf '%s' {int(seq)} > {shlex.quote(marker_path)}; }} 2>/dev/null; "
    return (
        f"{marker}"
        f"date +%s > {q}/started.t && mv {q}/started.t {q}/started; "
        f"setsid sh -c {shlex.quote(inner)} >/dev/null 2>&1 </dev/null & "
        f"echo sdlcma-issued"
    )


def wait_script(d: str, timeout_s: int) -> str:
    """Block until `rc` appears, then reproduce upstream's captured output.

    Bounded by the container's own `timeout` so a command that never finishes
    does not leave a polling shell spinning for the life of the container. The
    bail-out exit code is only a hint; the caller re-probes to decide, because
    no exit code can be reserved — the command's own may be anything.
    """
    q = shlex.quote(d)
    return (
        f"timeout {int(timeout_s)} sh -c 'while [ ! -f {q}/rc ]; do sleep 0.05; done' "
        f"2>/dev/null; "
        f"[ -f {q}/rc ] || exit {_WAITER_TIMEOUT_RC}; "
        f"cat {q}/out; exit \"$(cat {q}/rc)\""
    )


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
        # Validated here rather than at first use: a typo'd mode would
        # otherwise surface deep inside the loop, where the resume hook's own
        # exception guard would swallow it and silently degrade to no resume.
        self._ledger_mode = ledger_mode()
        self._ledger_root = ledger_dir()
        super().__init__(config_class=config_class, **kwargs)

    # ── state the agent side needs ───────────────────────────────────────────

    @property
    def seq(self) -> int:
        """Commands issued to this container so far (checkpointed as env_seq)."""
        return self._seq

    @property
    def attached(self) -> bool:
        return self._attached

    @property
    def ledger_enabled(self) -> bool:
        """Whether commands go through the per-command ledger (W2.5).

        getattr throughout for the same reason `_resume_active` uses it: this
        can be reached from `__del__` on a half-constructed object.
        """
        return (
            getattr(self, "_ledger_mode", "marker") == "ledger"
            and self._resume_active()
            and bool(getattr(self, "container_id", None))
        )

    def align(self, seq: int) -> None:
        """Restore the command counter after a restart.

        Called unconditionally on resume — including the "died at a clean
        boundary" case, which has no pending step. Without it the numbering
        restarts at 1 and walks straight into the previous incarnation's
        directories: the recorded command there is a different one, which reads
        as a mismatch and destroys a run that was perfectly recoverable. W2 got
        this for free because `reconcile()` did it as a side effect; the ledger
        path does not call `reconcile()`, so it has to be explicit.
        """
        self._seq = max(0, int(seq or 0))

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

        if rec and self._has_unfinished_agent_records(key):
            # ⚠ The container is gone but an UNFINISHED conversation is not.
            # Resuming those messages against a fresh container would hand the
            # model a context asserting edits that no longer exist — it would
            # "finish" and submit an empty diff, with nothing anywhere
            # reporting an error. So unfinished agent records die with the
            # container. Always.
            logger.warning("resume: checkpointed container %s is unusable — purging run %s",
                           rec.get("container_id"), key)
            self._store.purge_run(key)
        elif rec:
            # Every agent finished, so the container is gone because the
            # previous process released it on the way out — the normal end of a
            # mini loop, not a failure. Those `done` records are the node-output
            # memo (W2.5 §4.4): they let a worker that died during git apply /
            # push / CI wait replay a finished loop for nothing instead of
            # re-spending its whole trajectory. Purging here would delete
            # exactly the thing we kept them for.
            #
            # A container is still started, and immediately released, because
            # the agent short-circuits before using it. Avoiding that would
            # mean building the agent without an environment, which reaches a
            # lot further into mini than this feature is worth.
            logger.info("resume: run %s is finished; keeping its records for replay", key)

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

    def _has_unfinished_agent_records(self, key: str) -> bool:
        """Is any agent in this run still mid-conversation?

        The staged workflow modes run several agents against one environment,
        so "finished" is a property of the set, not of a known single document.
        """
        for name in self._store.names(key):
            if not name.startswith("agent-"):
                continue
            doc = self._store.load(key, name)
            if doc is not None and doc.get("status") != "done":
                return True
        return False

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

    def _docker_in(self, data: str, *args: str, timeout: int = 60) -> bool:
        """Like `_docker`, but feeds `data` to the command's stdin.

        This is how a command body reaches the container without passing
        through a shell quoting layer.
        """
        try:
            result = subprocess.run(
                [self.config.executable, *args],
                input=data, capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="surrogateescape",
            )
        except Exception as exc:
            logger.warning("resume: docker %s (stdin) failed: %s", args[0] if args else "?", exc)
            return False
        return result.returncode == 0

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

        **Any marker ahead of the record is ambiguous, by however much.** The
        first version treated a gap of exactly 1 as ambiguous and anything
        larger as "wrong container", on the assumption that one loop iteration
        issues at most one command. That assumption is false: mini's
        `execute_actions()` runs *every* action in an assistant message
        (`for action in message["extra"]["actions"]`), and models routinely emit
        two bash calls in one message — measured on ls4900, ~25% of steps. A
        kill landing after the first command of such a step left marker ==
        env_seq + 2, which the old rule read as a foreign container: it purged
        the record and aborted a run that was perfectly recoverable (L2c,
        astropy__astropy-14309). Widening costs nothing, because the whole
        un-checkpointed step is replayed regardless — its assistant message was
        never persisted, so the LLM call and *all* of its commands are re-issued
        either way. The gap only affects bookkeeping (`pending_commands`) and
        the decision to abort.

        Container identity is not this method's job: the run fingerprint plus
        the image/cwd check in `_attach` already cover it, and a genuinely
        foreign container has no marker file at all — which is still MISMATCH.
        """
        self._seq = int(env_seq or 0)
        self._pending_commands = 0
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
        self._pending_commands = marker - self._seq
        return AMBIGUOUS

    @property
    def pending_commands(self) -> int:
        """How many commands the container started that the record never saw."""
        return getattr(self, "_pending_commands", 0)

    # ── command ledger (W2.5) ────────────────────────────────────────────────

    def _cmd_dir(self, seq: int) -> str:
        return f"{self._ledger_root}/{int(seq)}"

    def _probe(self, seq: int) -> Probe:
        return parse_probe(self._docker(
            "exec", self.container_id, *_BOOKKEEPING, probe_script(self._cmd_dir(seq))))

    def _read_file(self, path: str) -> str:
        out = self._docker("exec", self.container_id, *_BOOKKEEPING,
                           f"cat {shlex.quote(path)} 2>/dev/null")
        return out or ""

    def _issue(self, seq: int, command: str, cwd: str) -> None:
        d = self._cmd_dir(seq)
        if not self._docker_in(command, "exec", "-i", self.container_id,
                               *_BOOKKEEPING, write_cmd_script(d)):
            raise LedgerMismatch(f"could not record command #{seq} in the container")
        # The environment flags belong on THIS exec: the detached command
        # inherits them, and dropping them would lose PYTHONUNBUFFERED /
        # PYTHONHASHSEED — the two settings that took ver99's replay hit rate
        # from 48% to 98.5%.
        args = ["exec", "-w", cwd]
        for key in self.config.forward_env:
            if (value := os.getenv(key)) is not None:
                args.extend(["-e", f"{key}={value}"])
        for key, value in self.config.env.items():
            args.extend(["-e", f"{key}={value}"])
        args.append(self.container_id)
        script = launch_script(d, seq, self._marker_path, list(self.config.interpreter),
                               self._marker_enabled)
        if self._docker(*args, *_BOOKKEEPING, script) is None:
            raise LedgerMismatch(f"could not launch command #{seq} in the container")

    def _harvest(self, seq: int, rc: int) -> dict:
        """A completed command's own output — no re-execution, ever."""
        return {"output": self._read_file(f"{self._cmd_dir(seq)}/out"),
                "returncode": rc, "exception_info": ""}

    def _timeout_output(self, seq: int, cmd: list, budget: float) -> dict:
        """Reproduce upstream's timeout observation, including partial output.

        Upstream times out the `docker exec` CLIENT and hands the model whatever
        the command had printed so far; the process inside the container keeps
        running. Both halves are reproduced here: the partial capture comes from
        `out`, and nothing is killed. Adding a container-side `timeout` would
        change `/testbed` state relative to upstream, and with it every later
        observation.
        """
        exc = subprocess.TimeoutExpired(cmd, budget)
        return {
            "output": self._read_file(f"{self._cmd_dir(seq)}/out"),
            "returncode": -1,
            "exception_info": f"An error occurred while executing the command: {exc}",
            "extra": {"exception_type": type(exc).__name__, "exception": str(exc)},
        }

    def _exec_capture(self, script: str, timeout: float) -> tuple[int, str] | None:
        """Run a bookkeeping script and capture it the way upstream captures a
        command: stderr merged into stdout. `None` means the client timed out.

        Merging matters for one case that is easy to miss: if the container has
        gone away, docker's own error text is what upstream would have put in
        the observation, and the model should see the same thing here.
        """
        cmd = [self.config.executable, "exec", self.container_id, *_BOOKKEEPING, script]
        try:
            result = subprocess.run(
                cmd, text=True, timeout=timeout, encoding="utf-8", errors="replace",
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
        except subprocess.TimeoutExpired:
            return None
        return result.returncode, result.stdout

    def _wait(self, seq: int, *, deadline: float, budget: float) -> dict:
        d = self._cmd_dir(seq)
        remaining = deadline - time.time()
        if remaining > 0:
            in_container = max(1, int(remaining))
            try:
                got = self._exec_capture(wait_script(d, in_container),
                                         in_container + _WAITER_SLACK_S)
                if got is not None and got[0] != _WAITER_TIMEOUT_RC:
                    return {"output": got[1], "returncode": got[0], "exception_info": ""}
            except Exception as exc:
                raw = getattr(exc, "output", None)
                raw = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else (raw or "")
                return {"output": raw, "returncode": -1,
                        "exception_info": f"An error occurred while executing the command: {exc}",
                        "extra": {"exception_type": type(exc).__name__, "exception": str(exc)}}
        # Either the waiter bailed out or the deadline had already passed on
        # entry (a command issued before a crash). `rc` decides, and it decides
        # FIRST: a command that finished while the worker was dead must be
        # harvested as the success it was, not reported as a timeout.
        st = self._probe(seq)
        if st.rc is not None:
            return self._harvest(seq, st.rc)
        return self._timeout_output(
            seq, [self.config.executable, "exec", self.container_id,
                  *self.config.interpreter, f"<ledger #{seq}>"], budget)

    def _on_mismatch(self, seq: int) -> None:
        key = self.config.sdlcma_resume_key
        logger.error("resume: ledger slot #%d holds a different command — the container and "
                     "the record have come apart. Purging run %s so the next attempt starts "
                     "clean.", seq, key)
        try:
            self._store.purge_run(key)
        except Exception:
            logger.warning("resume: failed to purge %s after mismatch", key, exc_info=True)
        raise LedgerMismatch(f"ledger slot #{seq} holds a different command for {key}")

    def _same_command(self, st: Probe, seq: int, command: str) -> bool:
        want = hashlib.sha256(command.encode("utf-8", errors="surrogateescape")).hexdigest()
        if st.cmd_sha256:
            return st.cmd_sha256 == want
        # No `sha256sum` in this image. Fall back to comparing the body itself
        # rather than refusing to run — this path is rare enough that the extra
        # exec and the decoding risk are acceptable, and an unverifiable slot is
        # not a reason to abandon a recoverable run.
        return self._read_file(f"{self._cmd_dir(seq)}/cmd") == command

    # ── command execution ────────────────────────────────────────────────────

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None):
        # The counter advances even when the marker is off, so `env_seq` stays a
        # faithful command count for the record either way.
        self._seq += 1
        if not self.ledger_enabled:
            if self._marker_enabled:
                action = {**action, "command": marker_command(
                    self._seq, self._marker_path, action.get("command", "") or "")}
            return super().execute(action, cwd, timeout=timeout)

        seq = self._seq
        command = action.get("command", "") or ""
        budget = float(timeout or self.config.timeout)
        st = self._probe(seq)

        if st.present and not self._same_command(st, seq, command):
            self._on_mismatch(seq)          # raises
        if not st.present or st.started is None:
            # Either nothing was ever written here, or the body was recorded but
            # the launch never happened (killed between the two execs). Both
            # mean the command has not run: re-issuing is safe, and waiting
            # would burn a whole timeout on a command that does not exist.
            self._issue(seq, command, cwd or self.config.cwd)
            st = self._probe(seq)

        # The deadline is anchored to THIS command's own start time, read back
        # from the container. The step-level `issued_at` on the pending record
        # would be wrong for every command after the first: a step can issue
        # several, and the later ones start after the earlier ones finish, so
        # charging them the step's elapsed time invents timeouts that never
        # happened — and a timeout observation is a different observation.
        started = st.started if st.started is not None else time.time()
        output = self._wait(seq, deadline=started + budget, budget=budget)
        self._check_finished(output)
        return output

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
