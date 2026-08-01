"""Command ledger — exactly-once command execution across a restart (W2.5).

Three groups, deliberately separated by what they can prove:

A. the container-side scripts, exercised against a REAL shell. They are the
   part that cannot be reasoned about on paper — quoting depth, whether `rc`
   survives the client being killed, whether a timed-out waiter leaves a
   process spinning.
B. `execute()`'s decision matrix, against an in-memory container. One case per
   row, and the assertion that matters everywhere is "was the command issued
   again?", because that is what exactly-once means.
C. the agent side — the half-step (`pending`) record, resuming without asking
   the model again, the finished-run memo, and idempotence across repeated
   crashes — against a ledger backed by real files.

See /mnt/d/PL/sdlcma/W3-command-ledger-design.md §4.0, §4.2, §4.4, §8.1.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "bf_worker"))

pytest.importorskip("minisweagent", reason="upstream mini-swe-agent not installed")

from agents import mini_swe_agent as M  # noqa: E402
from agents.base import BugInput  # noqa: E402
from agents.mini_resume_env import (  # noqa: E402
    LedgerMismatch,
    Probe,
    ResumableDockerEnvironment,
    ResumableDockerEnvironmentConfig,
    launch_script,
    ledger_mode,
    parse_probe,
    probe_script,
    wait_script,
    write_cmd_script,
)
from services.step_checkpoint import FileStore, purge_run_records  # noqa: E402

from minisweagent.environments.local import LocalEnvironment  # noqa: E402
from minisweagent.exceptions import Submitted  # noqa: E402
from minisweagent.models.test_models import DeterministicModel, make_output  # noqa: E402


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── A. the container-side scripts, against a real shell ──────────────────────
#
# `sh`, `setsid` and `timeout` are the only assumptions, and every SWE-bench
# image has them. Running them here rather than only under docker keeps the
# feedback loop short: these scripts are the part most likely to be wrong.

@pytest.fixture()
def slot(tmp_path):
    return str(tmp_path / "ledger" / "7")


def _sh(script: str, **kw):
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, **kw)


def test_a_command_body_survives_quoting_hell(slot):
    """The body never passes through a shell quoting layer — it goes by stdin.

    Two nested shells stand between us and the command; any command containing
    quotes, `$`, or a newline would eventually be mangled by string building.
    """
    body = "printf 'a $NOT_EXPANDED \"q\" `b`\\n'; echo 'multi\nline'"
    _sh(write_cmd_script(slot), input=body, check=True)
    assert Path(slot, "cmd").read_text() == body


def test_output_merges_stderr_and_preserves_the_exit_code(slot):
    _sh(write_cmd_script(slot), input="echo out; echo err >&2; exit 3", check=True)
    _sh(launch_script(slot, 7, str(Path(slot).parent / "m"), ["bash", "-lc"], True), check=True)
    result = _sh(wait_script(slot, 10))
    assert result.returncode == 3
    assert result.stdout == "out\nerr\n"
    assert Path(Path(slot).parent, "m").read_text() == "7"   # W2's marker still written


def test_the_command_outlives_the_client_that_started_it(slot):
    """The reason this design exists.

    The waiter is killed exactly the way a `docker exec` client dies when the
    worker is SIGKILLed. The command must keep running, finish, and record its
    own `rc` — otherwise there is nothing to harvest and resume is back to
    re-running things.
    """
    _sh(write_cmd_script(slot), input="echo START; sleep 2; echo END", check=True)
    _sh(launch_script(slot, 7, "/dev/null", ["bash", "-lc"], False), check=True)

    waiter = subprocess.Popen(["sh", "-c", wait_script(slot, 30)], stdout=subprocess.PIPE)
    time.sleep(0.4)
    waiter.kill()
    waiter.wait()
    assert not Path(slot, "rc").exists(), "precondition: the command is still running"

    time.sleep(2.4)
    assert Path(slot, "rc").read_text().strip() == "0"
    assert Path(slot, "out").read_text() == "START\nEND\n"


def test_a_waiter_that_times_out_gives_up_instead_of_spinning(slot):
    """Bounded in the container, so a hung command cannot leave a polling shell
    running for the container's whole two-hour life."""
    _sh(write_cmd_script(slot), input="echo partial; sleep 30", check=True)
    _sh(launch_script(slot, 7, "/dev/null", ["bash", "-lc"], False), check=True)

    t0 = time.time()
    result = _sh(wait_script(slot, 1))
    assert result.returncode == 99          # the bail-out hint
    assert time.time() - t0 < 5
    assert Path(slot, "out").read_text() == "partial\n"   # readable for the timeout path


def test_probe_reports_the_whole_state_in_one_shot(slot):
    assert parse_probe(_sh(probe_script(slot)).stdout) == Probe(present=False)

    _sh(write_cmd_script(slot), input="echo hi", check=True)
    recorded = parse_probe(_sh(probe_script(slot)).stdout)
    assert recorded.present and recorded.cmd_sha256 == _sha("echo hi")
    assert recorded.started is None, "written but never launched — §4.2 row 4"

    _sh(launch_script(slot, 7, "/dev/null", ["bash", "-lc"], False), check=True)
    time.sleep(0.4)
    done = parse_probe(_sh(probe_script(slot)).stdout)
    assert done.started is not None and done.rc == 0 and done.out_size == 3


def test_parse_probe_tolerates_half_written_fields():
    """Both files that matter are written by same-directory rename, so this is
    unreachable — but being lenient costs one re-issue, and being strict costs
    the run."""
    got = parse_probe("CMD abc\nSTARTED \nRC \nSIZE oops\n")
    assert got == Probe(present=True, cmd_sha256="abc", started=None, rc=None, out_size=0)


def test_the_command_body_never_reaches_a_script():
    """A guard against the obvious "simplification".

    No generator takes the command body, so none of them can mangle it: the
    launcher reads it back from the file the body was streamed into.
    """
    import inspect

    for fn in (probe_script, wait_script, launch_script, write_cmd_script):
        taken = set(inspect.signature(fn).parameters)
        assert not (taken & {"command", "body", "cmd"}), f"{fn.__name__} takes the body"

    launch = launch_script("/L/1", 1, "/m", ["bash", "-lc"], False)
    assert '"$(cat /L/1/cmd)"' in launch
    assert "setsid" in launch, "the command must outlive this exec client"
    assert "/L/1/rc.t" in launch and "mv /L/1/rc.t /L/1/rc" in launch


def test_ledger_mode_rejects_an_unknown_value(monkeypatch):
    monkeypatch.setenv("BF_STEP_LEDGER", "sure-why-not")
    with pytest.raises(ValueError, match="BF_STEP_LEDGER"):
        ledger_mode()


# ── B. execute()'s decision matrix, against an in-memory container ───────────

class _FakeLedgerEnv(ResumableDockerEnvironment):
    """Replaces the three docker seams with a dict. Everything else is real."""

    def __init__(self, store, *, root="/L", auto_complete=True):
        self.config = ResumableDockerEnvironmentConfig(
            image="img", cwd="/testbed", timeout=30,
            sdlcma_resume_key="K", sdlcma_run_fingerprint="fp",
        )
        self._store = store
        self._seq = 0
        self._released = False
        self._attached = True
        self._marker_enabled = False
        self._marker_path = "/dev/shm/.sdlcma_step"
        self._ledger_mode = "ledger"
        self._ledger_root = root
        self.container_id = "cid"
        self.slots: dict[int, dict] = {}
        self.issued: list[int] = []
        self.auto_complete = auto_complete

    # -- seams -------------------------------------------------------------
    def _seq_of(self, script: str) -> int:
        return int(re.search(rf"{re.escape(self._ledger_root)}/(\d+)", script).group(1))

    def _docker(self, *args, timeout=60):
        script = args[-1]
        seq = self._seq_of(script)
        slot = self.slots.get(seq)
        if script.startswith("cd "):                       # probe
            if slot is None:
                return "ABSENT\n"
            text = f"CMD {_sha(slot['cmd'])}\n"
            if "started" in slot:
                text += f"STARTED {slot['started']}\n"
            if "rc" in slot:
                text += f"RC {slot['rc']}\n"
            return text + f"SIZE {len(slot.get('out', ''))}\n"
        if script.startswith("cat "):                      # _read_file
            key = "cmd" if script.split()[1].endswith("/cmd") else "out"
            return (slot or {}).get(key, "")
        if "setsid" in script:                             # launch
            self.issued.append(seq)
            slot["started"] = time.time()
            if self.auto_complete:
                slot["out"], slot["rc"] = f"ran #{seq}\n", 0
            return "sdlcma-issued\n"
        raise AssertionError(f"unexpected script: {script}")

    def _docker_in(self, data, *args, timeout=60):
        self.slots[self._seq_of(args[-1])] = {"cmd": data}   # rm -rf semantics
        return True

    def _exec_capture(self, script, timeout):
        slot = self.slots.get(self._seq_of(script))
        if slot is None or "rc" not in slot:
            return (99, "")
        return (slot["rc"], slot["out"])


@pytest.fixture()
def store(tmp_path):
    return FileStore(tmp_path / "records")


@pytest.fixture()
def env(store):
    return _FakeLedgerEnv(store)


def _run_cmd(env, command="echo hi", **kw):
    return env.execute({"command": command}, **kw)


def test_row2_nothing_recorded_runs_the_command(env):
    out = _run_cmd(env)
    assert env.issued == [1]
    assert out == {"output": "ran #1\n", "returncode": 0, "exception_info": ""}


def test_row3_a_different_command_in_the_slot_is_fatal(env, store):
    env.slots[1] = {"cmd": "something else", "started": time.time(), "rc": 0, "out": "x"}
    with pytest.raises(LedgerMismatch):
        _run_cmd(env)
    assert env.issued == [], "must not run anything against a container it cannot explain"
    assert store.load("K", "agent-0") is None, "the run's records are purged"


def test_row4_recorded_but_never_launched_is_reissued(env):
    """Killed between the two execs: `cmd` is there, `started` is not.

    Without this row the command reads as "running", and we would burn a whole
    timeout waiting for something that never started.
    """
    env.slots[1] = {"cmd": "echo hi"}
    out = _run_cmd(env)
    assert env.issued == [1]
    assert out["returncode"] == 0


def test_row5_a_finished_command_is_harvested_not_rerun(env):
    """The headline: the container ran it, the agent never got the answer."""
    env.slots[1] = {"cmd": "echo hi", "started": time.time() - 5,
                    "rc": 7, "out": "the original output\n"}
    out = _run_cmd(env)
    assert env.issued == [], "exactly-once"
    assert out == {"output": "the original output\n", "returncode": 7, "exception_info": ""}


def test_row6_a_running_command_is_waited_for(env):
    env.slots[1] = {"cmd": "echo hi", "started": time.time()}

    def complete_on_wait(script, timeout):
        env.slots[1].update(out="late output\n", rc=0)
        return (0, "late output\n")

    env._exec_capture = complete_on_wait
    out = _run_cmd(env)
    assert env.issued == []
    assert out["output"] == "late output\n"


def test_row7_a_command_past_its_deadline_times_out_like_upstream(env):
    """Upstream hands the model the partial output and leaves the process
    running; both halves are reproduced."""
    env.slots[1] = {"cmd": "echo hi", "started": time.time() - 3600, "out": "partial\n"}
    out = _run_cmd(env)
    assert env.issued == []
    assert out["returncode"] == -1
    assert out["output"] == "partial\n"
    assert "timed out after" in out["exception_info"]
    assert out["extra"]["exception_type"] == "TimeoutExpired"
    assert "rc" not in env.slots[1], "nothing was killed"


def test_rc_wins_over_the_deadline(env):
    """A command that finished while the worker was dead is a success, not a
    timeout. Reporting the timeout would swap a real observation for a false
    one, and observations are what the model reasons from."""
    env.slots[1] = {"cmd": "echo hi", "started": time.time() - 3600,
                    "rc": 0, "out": "finished while we were away\n"}
    out = _run_cmd(env)
    assert out["returncode"] == 0 and out["output"] == "finished while we were away\n"


def test_the_deadline_is_per_command_not_per_step(env):
    """The bug this row exists for: a step can issue several commands, and the
    later ones start after the earlier ones finish. Charging command #2 the
    step's elapsed time invents a timeout that never happened."""
    step_started = time.time() - 25            # cmd #1 ran for 25s of a 30s budget
    env.slots[1] = {"cmd": "one", "started": step_started, "rc": 0, "out": "1\n"}
    env.slots[2] = {"cmd": "two", "started": time.time(), "rc": 0, "out": "2\n"}
    assert _run_cmd(env, "one")["returncode"] == 0
    second = _run_cmd(env, "two")
    assert second["returncode"] == 0, "cmd #2 must be judged against its own start time"
    assert second["output"] == "2\n"


def test_a_missing_sha256sum_falls_back_to_the_body(env):
    """An image without coreutils is not a reason to abandon a recoverable run."""
    env.slots[1] = {"cmd": "echo hi", "started": time.time(), "rc": 0, "out": "x\n"}
    original = env._docker

    def no_sha(*args, timeout=60):
        out = original(*args, timeout=timeout)
        return out.replace(f"CMD {_sha('echo hi')}", "CMD ") if args[-1].startswith("cd ") else out

    env._docker = no_sha
    assert _run_cmd(env)["returncode"] == 0
    assert env.issued == []


def test_submission_is_detected_on_the_harvest_path(env):
    env.slots[1] = {"cmd": "submit", "started": time.time(), "rc": 0,
                    "out": "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\ndiff --git a/x b/x\n"}
    with pytest.raises(Submitted):
        _run_cmd(env, "submit")


def test_align_restores_the_counter_across_a_restart(env):
    """§4.4bis's trap. Without align() the numbering restarts at 1 and walks
    into the previous incarnation's slots, where a different command is
    recorded — read as a mismatch, and a recoverable run is destroyed."""
    env.slots[1] = {"cmd": "old #1", "started": 1.0, "rc": 0, "out": "old\n"}
    env.align(1)
    env.slots[2] = {"cmd": "new", "started": time.time(), "rc": 0, "out": "new\n"}
    assert _run_cmd(env, "new")["output"] == "new\n"


def test_the_ledger_is_off_unless_resume_is_on(store):
    env = _FakeLedgerEnv(store)
    env.config.sdlcma_resume_key = ""          # resume disabled
    assert env.ledger_enabled is False


def test_marker_mode_does_not_touch_the_ledger(store, monkeypatch):
    monkeypatch.setenv("BF_STEP_LEDGER", "marker")
    env = _FakeLedgerEnv(store)
    env._ledger_mode = ledger_mode()
    assert env.ledger_enabled is False


# ── C. the agent side, against a file-backed ledger ─────────────────────────

class _Killed(BaseException):
    """An external kill: deliberately not an Exception, so mini's loop cannot
    catch it and record the run as finished."""


class _LedgerLocalEnv(LocalEnvironment):
    """A real local environment plus a real (file-backed) command ledger.

    Mirrors `ResumableDockerEnvironment`'s ledger semantics closely enough to
    test the agent side without docker, and counts actual executions — which is
    how "exactly once" gets asserted rather than asserted-about.
    """

    ledger_enabled = True

    def __init__(self, root: Path, *, die_at_command=None, **kwargs):
        super().__init__(**kwargs)
        self.root = Path(root)
        self._seq = 0
        self.executions: list[str] = []
        self.harvests: list[int] = []
        self._die_at_command = die_at_command
        self.released = False

    @property
    def seq(self) -> int:
        return self._seq

    def align(self, seq: int) -> None:
        self._seq = int(seq)

    def release(self) -> None:
        self.released = True

    def execute(self, action, cwd="", *, timeout=None):
        self._seq += 1
        seq, command = self._seq, action.get("command", "") or ""
        slot = self.root / str(seq)
        if slot.exists():
            if (slot / "cmd").read_text() != command:
                raise LedgerMismatch(f"slot #{seq}")
            if (slot / "rc").exists():
                self.harvests.append(seq)
                output = {"output": (slot / "out").read_text(),
                          "returncode": int((slot / "rc").read_text()), "exception_info": ""}
                self._check_finished(output)
                return output
        else:
            slot.mkdir(parents=True)
            (slot / "cmd").write_text(command)
        if self._die_at_command == seq:
            raise _Killed(f"killed during command #{seq}")
        # The real ledger writes rc inside the container before anything can
        # observe the output, so submission detection has to happen after it.
        self._check_finished = lambda output: None
        try:
            result = super().execute(action, cwd, timeout=timeout)
        finally:
            del self._check_finished
        self.executions.append(command)
        (slot / "out").write_text(result["output"])
        (slot / "rc").write_text(str(result["returncode"]))
        self._check_finished(result)
        return result


_INSTANCE = {"instance_id": "sympy__sympy-1", "problem_statement": "x is off by one"}
_AGENT_CFG = {"system_template": "You are helpful.", "instance_template": "{{task}}",
              "step_limit": 20, "cost_limit": 0}
_SUBMIT = ("printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\n"
           "diff --git a/x.py b/x.py\\n+    return n - 1\\n'")
_SCRIPT = [("look", "echo step1"), ("read", "echo step2"), ("edit", "echo step3"),
           ("verify", "echo step4"), ("submit", _SUBMIT)]
# Modes 1/3 build several agents on ONE environment, so the ledger's sequence
# numbering and the per-agent records have to coexist. Keyed on a phrase that
# really occurs in that phase's system prompt; "" is the fallback and must stay
# last, being a substring of everything.
_PHASED = {
    "investigating a bug": [
        ("investigating", "echo investigating"),
        ("hand off", "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\nROOT_CAUSE: off by one\\n'"),
    ],
    "": [("fixing", "echo fixing"), ("verify", "echo verify"), ("submit patch", _SUBMIT)],
}
_TWO_COMMANDS = [("look", ["echo one", "echo two"]), ("submit", [_SUBMIT])]


class _ScriptedModel(DeterministicModel):
    """Answers as a pure function of the conversation, like the replay cache.

    A positional script would replay from the top after a resume (the model is
    a fresh object), which would be an artefact of the fake rather than of the
    code under test. A dict script selects by phase, the way the staged modes
    give each phase its own system prompt.
    """

    def __init__(self, *, script, die_before_query=None, **kwargs):
        super().__init__(outputs=[], **kwargs)
        self._script = script
        self.queries = 0
        self._die_before_query = die_before_query

    def _script_for(self, messages) -> list:
        if isinstance(self._script, list):
            return self._script
        system = (messages[0].get("content") or "") if messages else ""
        for key, script in self._script.items():
            if key in system:
                return script
        return self._script[""]

    def query(self, messages, **kwargs):
        self.queries += 1
        if self._die_before_query == self.queries:
            raise _Killed(f"killed before query #{self.queries}")
        turn = sum(1 for m in messages if m.get("role") == "assistant")
        thought, commands = self._script_for(messages)[turn]
        if isinstance(commands, str):
            commands = [commands]
        self.config.outputs = [make_output(thought, [{"command": c} for c in commands])]
        self.current_index = -1
        return super().query(messages, **kwargs)


@dataclass
class _Attempt:
    result: object = None
    model: object = None
    env: object = None
    agent: object = None
    raised: BaseException | None = None
    agents: list = field(default_factory=list)


def _attempt(tmp_path, *, bug_id="BUG-1", env=None, script=None, mode=0,
             die_before_query=None, die_at_command=None) -> _Attempt:
    model = _ScriptedModel(script=script or _SCRIPT, die_before_query=die_before_query)
    if env is None:
        (tmp_path / "work").mkdir(parents=True, exist_ok=True)
        env = _LedgerLocalEnv(tmp_path / "ledger", cwd=str(tmp_path / "work"),
                              die_at_command=die_at_command)
    else:
        env._die_at_command = die_at_command
    agent = M.MiniSweAgent(model=model, env=env, workflow_mode=mode,
                           mini_config={"agent": dict(_AGENT_CFG)})
    out = _Attempt(model=model, env=env, agent=agent)

    original = M.MiniSweAgent._construct_agent

    def _spy(self, model_, env_, cfg):
        built = original(self, model_, env_, cfg)
        out.agents.append(built)
        return built

    M.MiniSweAgent._construct_agent = _spy
    try:
        out.result = agent.fix(BugInput(bug_id=bug_id, provider=None,
                                        metadata={"swebench_instance": _INSTANCE}))
    except BaseException as exc:   # noqa: BLE001 — _Killed is the point
        out.raised = exc
    finally:
        M.MiniSweAgent._construct_agent = original
    return out


@pytest.fixture(autouse=True)
def _ledger_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_IMPL", "vendored")     # resume needs the vendored seams
    monkeypatch.setenv("BF_STEP_CHECKPOINT", "file")
    monkeypatch.setenv("BF_STEP_CHECKPOINT_DIR", str(tmp_path / "records"))
    monkeypatch.delenv("BF_STEP_LEDGER", raising=False)
    monkeypatch.delenv("BF_STEP_ON_AMBIGUOUS", raising=False)


@pytest.fixture()
def records(tmp_path):
    return FileStore(tmp_path / "records")


def test_the_half_step_is_persisted_before_any_command_runs(tmp_path, records):
    """`pending` must exist by the time the first command is issued — that is
    the whole ordering the ledger depends on."""
    killed = _attempt(tmp_path, die_at_command=3)
    assert isinstance(killed.raised, _Killed)

    rec = records.load("BUG-1", "agent-0")
    assert rec["pending"] is not None
    assert rec["pending"]["message"]["role"] == "assistant"
    assert rec["pending"]["first_seq"] == 3
    assert rec["messages"][-1]["role"] != "assistant", "the prefix stops at the boundary"


def test_resume_does_not_ask_the_model_again(tmp_path):
    """The half-step's whole point. A re-query could answer differently, and
    then the commands already executed belong to a trajectory that no longer
    exists."""
    reference = _attempt(tmp_path / "ref", bug_id="REF")
    assert reference.result.outcome == "fixed"

    first = _attempt(tmp_path, die_at_command=3)
    second = _attempt(tmp_path, env=first.env)

    assert second.result.outcome == "fixed"
    # Five steps in total; the interrupted one is NOT re-asked, so the second
    # attempt only makes the calls the first never got to.
    assert first.model.queries + second.model.queries == 5
    assert second.result.final_state["model_patch"] == \
        reference.result.final_state["model_patch"]


def test_an_interrupted_command_is_not_executed_twice(tmp_path):
    """Exactly-once, counted where it actually happens."""
    first = _attempt(tmp_path, die_at_command=3)
    second = _attempt(tmp_path, env=first.env)
    assert second.result.outcome == "fixed"

    ran = first.env.executions
    assert len(ran) == len(set(ran)), f"a command ran twice: {ran}"
    assert second.result.final_state["step_replayed_command_count"] == 0


def test_a_multi_command_step_resumes_per_command(tmp_path, records):
    """Two commands in one message, killed between them: the first is
    harvested, the second is issued. W2's single marker number could not tell
    these two states apart."""
    first = _attempt(tmp_path, script=_TWO_COMMANDS, die_at_command=2)
    assert isinstance(first.raised, _Killed)
    assert first.env.executions == ["echo one"]

    second = _attempt(tmp_path, env=first.env, script=_TWO_COMMANDS)
    assert second.result.outcome == "fixed"
    assert first.env.harvests == [1], "command #1 came back from the ledger"
    assert first.env.executions.count("echo one") == 1
    assert first.env.executions.count("echo two") == 1

    messages = second.agents[0].messages
    observations = [m for m in messages if m.get("role") == "user"][1:]
    assert len(observations) >= 2


def test_two_crashes_including_one_inside_a_resumed_step(tmp_path):
    """§4.4bis: `pending` is immutable for the life of the step, so the second
    resume reconciles from the same record and reaches the same conclusions."""
    first = _attempt(tmp_path, die_at_command=2)
    second = _attempt(tmp_path, env=first.env, die_at_command=4)
    third = _attempt(tmp_path, env=first.env)

    assert third.result.outcome == "fixed"
    assert third.result.final_state["step_resume_count"] == 2
    assert third.result.final_state["step_replayed_command_count"] == 0
    ran = first.env.executions
    assert len(ran) == len(set(ran)), f"a command ran twice across two crashes: {ran}"


def test_a_boundary_crash_keeps_the_command_numbering(tmp_path):
    """The trap: with no pending step there is nothing to align against, so the
    counter must be restored from the boundary record. Restarting at 1 would
    walk into the previous incarnation's slots and read as a mismatch."""
    first = _attempt(tmp_path, die_before_query=3)
    assert first.env.seq == 2
    second = _attempt(tmp_path, env=first.env)
    assert second.result.outcome == "fixed"
    assert second.env.seq == 5, "numbering continued instead of restarting"


def test_the_finished_loop_is_replayed_for_free(tmp_path, records):
    """The memo (§4.4): a worker killed AFTER the loop — during git apply, push
    or the CI wait — must not re-run the trajectory."""
    done = _attempt(tmp_path)
    assert done.result.outcome == "fixed"
    assert records.load("BUG-1", "agent-0")["status"] == "done"

    ran_before = list(done.env.executions)
    replay = _attempt(tmp_path, env=done.env)
    assert replay.model.queries == 0, "not one LLM call"
    assert replay.env.executions == ran_before, "not one command"
    assert replay.result.final_state["model_patch"] == done.result.final_state["model_patch"]


def test_the_memo_survives_the_container_being_gone(tmp_path, records):
    """W2 purged the whole run when the container could not be attached. After
    the loop that is the NORMAL state — the container was released on the way
    out — and purging there would delete the memo."""
    done = _attempt(tmp_path)
    assert records.load("BUG-1", "agent-0")["status"] == "done"
    # The env is not reused: a fresh one stands in for "the container is gone".
    replay = _attempt(tmp_path)
    assert replay.model.queries == 0
    assert replay.result.final_state["model_patch"] == done.result.final_state["model_patch"]


def test_finish_run_ends_the_memo(tmp_path, records):
    done = _attempt(tmp_path)
    done.agent.finish_run()
    assert records.load("BUG-1", "agent-0") is None

    again = _attempt(tmp_path)
    assert again.model.queries == 5, "with no record, the run starts over"


def test_purge_run_records_is_total(tmp_path, records, monkeypatch):
    """It runs in `finally` blocks, where raising would replace the real reason
    the process is exiting."""
    _attempt(tmp_path)
    assert records.load("BUG-1", "agent-0") is not None
    purge_run_records("BUG-1")
    assert records.load("BUG-1", "agent-0") is None

    purge_run_records("BUG-1")      # idempotent
    purge_run_records("")           # no key, no crash
    monkeypatch.setenv("BF_STEP_CHECKPOINT", "nonsense")
    purge_run_records("BUG-1")      # unusable config, still no crash


def test_records_are_untouched_when_the_feature_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv("BF_STEP_CHECKPOINT", "none")
    attempt = _attempt(tmp_path)
    assert attempt.result.outcome == "fixed"
    assert not (tmp_path / "records" / "BUG-1").exists()
    assert "step_resume_count" not in attempt.result.final_state


# ── C2. the staged modes: several agents, one environment, one ledger ───────
#
# Modes 1/3 run an Investigate agent and a Solve agent against the SAME docker
# environment, which is exactly where the per-agent records and the per-command
# ledger have to coexist: `agent-0` can be `done` while `agent-1` is mid-step,
# and the container-side sequence numbering runs straight through both.

def test_a_staged_run_gives_each_phase_its_own_record_and_one_ledger(tmp_path, records):
    killed = _attempt(tmp_path, mode=1, script=_PHASED, die_at_command=3)
    assert isinstance(killed.raised, _Killed)

    assert records.load("BUG-1", "agent-0")["status"] == "done", "phase 1 finished"
    solve = records.load("BUG-1", "agent-1")
    assert solve["status"] == "running"
    assert solve["pending"] is not None, "phase 2 was interrupted mid-step"
    # The ledger numbering does not restart per phase — command #3 belongs to
    # phase 2, and its slot must not collide with phase 1's.
    assert solve["pending"]["first_seq"] == 3
    assert sorted(p.name for p in (tmp_path / "ledger").iterdir()) == ["1", "2", "3"]


def test_a_staged_run_resumes_phase_two_without_replaying_phase_one(tmp_path):
    """The memo and the half-step, in one run: phase 1 replays from its record
    for nothing, phase 2 continues from the command it was killed on."""
    reference = _attempt(tmp_path / "ref", bug_id="REF", mode=1, script=_PHASED)
    assert reference.result.outcome == "fixed"

    first = _attempt(tmp_path, mode=1, script=_PHASED, die_at_command=3)
    second = _attempt(tmp_path, env=first.env, mode=1, script=_PHASED)

    assert second.result.outcome == "fixed"
    # Phase 1 is not re-asked (2 calls) and neither is the interrupted step.
    assert first.model.queries + second.model.queries == reference.model.queries
    assert second.result.final_state["model_patch"] == \
        reference.result.final_state["model_patch"]
    ran = first.env.executions
    assert len(ran) == len(set(ran)), f"a command ran twice across phases: {ran}"
    assert second.result.final_state["step_replayed_command_count"] == 0


def test_an_unfinished_phase_still_invalidates_a_dead_container(tmp_path, records, monkeypatch):
    """The narrowing in `_start_container` is per-RUN, not per-agent: `agent-0`
    being `done` must not make a lost container look harmless while `agent-1`
    still has a conversation asserting edits inside it."""
    from agents import mini_resume_env as mre

    _attempt(tmp_path, mode=1, script=_PHASED, die_at_command=3)
    env = mre.ResumableDockerEnvironment.__new__(mre.ResumableDockerEnvironment)
    env._store = records
    assert env._has_unfinished_agent_records("BUG-1") is True

    # …and once every phase is done, the container going away is the normal end
    # of the run, so the memo survives it.
    doc = records.load("BUG-1", "agent-1")
    doc["status"] = "done"
    records.save("BUG-1", "agent-1", doc)
    assert env._has_unfinished_agent_records("BUG-1") is False


def test_a_staged_run_that_finished_replays_both_phases_for_free(tmp_path, records):
    done = _attempt(tmp_path, mode=1, script=_PHASED)
    assert done.result.outcome == "fixed"
    assert records.load("BUG-1", "agent-0")["status"] == "done"
    assert records.load("BUG-1", "agent-1")["status"] == "done"

    ran_before = list(done.env.executions)
    replay = _attempt(tmp_path, env=done.env, mode=1, script=_PHASED)
    assert replay.model.queries == 0, "neither phase may be re-asked"
    assert replay.env.executions == ran_before
    assert replay.result.final_state["model_patch"] == done.result.final_state["model_patch"]
