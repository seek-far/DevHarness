"""U2-U6, U9-U12 — intra-loop resume (plan item W2).

No docker, no LLM, no network: a scripted model plus a local environment, with
the "worker was killed" event modelled as a `BaseException`. That choice is the
point — mini's loop catches `Exception`, so an ordinary exception would append
its own exit message and record the run as *finished*, which is precisely not
what an external kill does. A BaseException slips past that handler exactly the
way SIGKILL slips past everything.

The headline assertion (`test_resumed_run_matches_uninterrupted_run`) is that a
killed-and-resumed run ends with byte-identical messages to a run that was never
interrupted, having re-issued only the calls it actually lost.

See /mnt/d/PL/sdlcma/W2-step-checkpoint-design.md §5, §7.2, §11.
"""

from __future__ import annotations

import os
import sys
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
from agents.mini_resume import StepResumeAborted  # noqa: E402
from agents.mini_resume_env import AMBIGUOUS, CLEAN, MISMATCH, marker_command  # noqa: E402
from services.step_checkpoint import FileStore  # noqa: E402

from minisweagent.environments.local import LocalEnvironment  # noqa: E402
from minisweagent.models.test_models import DeterministicModel, make_output  # noqa: E402


_INSTANCE = {"instance_id": "sympy__sympy-1", "problem_statement": "x is off by one"}

_AGENT_CFG = {
    "system_template": "You are a helpful assistant.",
    "instance_template": "{{task}}",
    "step_limit": 20,
    "cost_limit": 0,   # DeterministicModel bills $1/call; the default cap truncates
}

_SUBMIT = (
    "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\n"
    "diff --git a/x.py b/x.py\\n+    return n - 1\\n'"
)

# Four working turns then a submission — long enough that "resume from step 3"
# is meaningfully different from "start over".
_SCRIPT = [
    ("look around", "echo step1"),
    ("read the file", "echo step2"),
    ("edit it", "echo step3"),
    ("verify", "echo step4"),
    ("submit", _SUBMIT),
]


class _Killed(BaseException):
    """Stands in for the worker being killed.

    Deliberately not an `Exception`: mini's loop would catch that, append an
    exit message and record the run as finished — the opposite of what an
    external kill does.
    """


class _CacheLikeModel(DeterministicModel):
    """Scripted model whose answer is a pure function of the conversation.

    Not a positional list: after a resume the model is a fresh object, and a
    positional script would replay from output #0 and produce a different
    conversation — an artefact of the fake, not of the code under test. Keying
    on the messages mirrors what the real gateway replay cache does, so the
    "same prefix ⇒ same answer" property this feature depends on is actually
    exercised. Phases are told apart by their system prompt, since the staged
    modes give each one its own.
    """

    def __init__(self, *, scripts, die_before_query=None, **kwargs):
        super().__init__(outputs=[], **kwargs)
        self._scripts = scripts            # {phase_key: [(thought, command), ...]}
        self.queries = 0
        self._die_before_query = die_before_query

    def _script_for(self, messages) -> list:
        system = (messages[0].get("content") or "") if messages else ""
        for key, script in self._scripts.items():
            if key in system:
                return script
        return self._scripts[""]

    def query(self, messages, **kwargs):
        self.queries += 1
        if self._die_before_query == self.queries:
            # Killed at a clean loop boundary: the previous iteration was fully
            # checkpointed and no new command has been issued.
            raise _Killed(f"killed before query #{self.queries}")
        turn = sum(1 for m in messages if m.get("role") == "assistant")
        thought, command = self._script_for(messages)[turn]
        self.config.outputs = [make_output(thought, [{"command": command}])]
        self.current_index = -1
        return super().query(messages, **kwargs)


class _TestEnv(LocalEnvironment):
    """LocalEnvironment + the resume protocol the agent talks to.

    `_marker` mirrors what the real container file holds: the sequence number is
    written as the command *starts*, so a kill during execution leaves the
    marker one ahead of the last checkpoint — which is exactly the ambiguity
    reconcile() has to report.
    """

    def __init__(self, *, die_at_command=None, **kwargs):
        super().__init__(**kwargs)
        self._seq = 0
        self._marker = 0
        self._die_at_command = die_at_command
        self.released = False

    @property
    def seq(self) -> int:
        return self._seq

    def execute(self, action, cwd="", *, timeout=None):
        self._seq += 1
        self._marker = self._seq
        if self._die_at_command == self._seq:
            raise _Killed(f"killed during command #{self._seq}")
        return super().execute(action, cwd, timeout=timeout)

    def reconcile(self, env_seq: int) -> str:
        # Mirrors ResumableDockerEnvironment.reconcile: ANY marker ahead of the
        # record is ambiguous — one iteration can issue several commands.
        self._seq = int(env_seq or 0)
        self._pending_commands = max(0, self._marker - self._seq)
        return CLEAN if self._marker <= self._seq else AMBIGUOUS

    @property
    def pending_commands(self) -> int:
        return getattr(self, "_pending_commands", 0)

    def release(self):
        self.released = True


# ── harness ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _resume_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_IMPL", "vendored")
    monkeypatch.setenv("BF_STEP_CHECKPOINT", "file")
    monkeypatch.setenv("BF_STEP_CHECKPOINT_DIR", str(tmp_path / "cp"))
    monkeypatch.delenv("BF_STEP_ON_AMBIGUOUS", raising=False)


@pytest.fixture
def store(tmp_path) -> FileStore:
    return FileStore(tmp_path / "cp")


def _cwd(tmp_path, name="work") -> str:
    # LocalEnvironment runs commands here; a missing directory makes every
    # command fail in a way that looks like a parity failure but is not.
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    return str(d)


@dataclass
class _Attempt:
    """One fix() call — the worker process, in miniature."""
    result: object = None
    model: object = None
    env: object = None
    raised: BaseException | None = None
    agents: list = field(default_factory=list)

    @property
    def messages(self) -> list:
        """The whole run's conversation: every agent's, in construction order."""
        return [m for a in self.agents for m in a.messages]


def _run(tmp_path, *, bug_id="BUG-1", die_before_query=None, die_at_command=None,
         env=None, scripts=None, mode=0):
    model = _CacheLikeModel(scripts=scripts or {"": _SCRIPT}, die_before_query=die_before_query)
    if env is None:
        env = _TestEnv(cwd=_cwd(tmp_path), die_at_command=die_at_command)
    else:
        # A reused env stands in for the surviving container, NOT for a
        # surviving kill switch — leaving it armed would kill every attempt at
        # the same command and never let the run finish.
        env._die_at_command = die_at_command
    agent = M.MiniSweAgent(
        model=model, env=env, workflow_mode=mode,
        mini_config={"agent": dict(_AGENT_CFG)},
    )
    attempt = _Attempt(model=model, env=env)

    # fix() does not hand back the mini agents it builds, and the assertions
    # here are about their conversations, so intercept the single construction
    # site all five call sites funnel through.
    original = M.MiniSweAgent._construct_agent

    def _spy(self, model_, env_, cfg):
        built = original(self, model_, env_, cfg)
        attempt.agents.append(built)
        return built

    M.MiniSweAgent._construct_agent = _spy
    bug = BugInput(bug_id=bug_id, provider=None, metadata={"swebench_instance": _INSTANCE})
    try:
        attempt.result = agent.fix(bug)
    except BaseException as exc:   # noqa: BLE001 - _Killed is intentional here
        attempt.raised = exc
    finally:
        M.MiniSweAgent._construct_agent = original
    return attempt


def _texts(messages):
    return [(m.get("role"), m.get("content")) for m in messages]


# ── U2: the headline case ────────────────────────────────────────────────────

def test_resumed_run_matches_uninterrupted_run(tmp_path, store):
    """Kill at a clean boundary, resume, and land in exactly the same place."""
    reference = _run(tmp_path, bug_id="REF")
    assert reference.result.outcome == "fixed"

    # Attempt 1 — dies just before the 4th LLM call, i.e. after 3 full iterations.
    first = _run(tmp_path, bug_id="BUG-1", die_before_query=4)
    assert isinstance(first.raised, _Killed)
    assert first.model.queries == 4 and first.env.seq == 3

    rec = store.load("BUG-1", "agent-0")
    assert rec["status"] == "running"
    assert (rec["step"], rec["env_seq"], rec["n_calls"]) == (3, 3, 3)

    # Attempt 2 — same env object, standing in for the surviving container.
    second = _run(tmp_path, bug_id="BUG-1", env=first.env)

    assert second.result.outcome == "fixed"
    # Only the calls that were actually lost get re-issued: 5 total, 3 restored.
    assert second.model.queries == 2
    assert second.result.iterations == reference.result.iterations == 5
    # And it is the *same* conversation, not merely a working one.
    assert _texts(second.messages) == _texts(reference.messages)
    assert second.result.final_state["model_patch"] == reference.result.final_state["model_patch"]


def test_resume_telemetry_lands_on_final_state(tmp_path):
    first = _run(tmp_path, bug_id="BUG-1", die_before_query=4)
    second = _run(tmp_path, bug_id="BUG-1", env=first.env)

    assert second.result.final_state["step_resume_count"] == 1
    assert second.result.final_state["step_resumed_from_step"] == 3
    assert second.result.final_state["step_replayed_command_count"] == 0


def test_two_kills_in_one_run_keep_counting(tmp_path):
    """resume_count is cumulative — it lives in the record, not the process."""
    first = _run(tmp_path, bug_id="BUG-1", die_before_query=3)
    second = _run(tmp_path, bug_id="BUG-1", env=first.env, die_before_query=2)
    third = _run(tmp_path, bug_id="BUG-1", env=second.env)

    assert third.result.outcome == "fixed"
    assert third.result.final_state["step_resume_count"] == 2
    assert third.result.iterations == 5


def test_a_run_that_was_never_killed_reports_no_resumes(tmp_path):
    attempt = _run(tmp_path, bug_id="BUG-1")
    assert attempt.result.final_state["step_resume_count"] == 0
    assert attempt.result.final_state["step_resumed_from_step"] is None


def test_telemetry_absent_when_the_feature_is_off(tmp_path, monkeypatch):
    """Off must read as "unknown", never as a confident zero."""
    monkeypatch.setenv("BF_STEP_CHECKPOINT", "none")
    attempt = _run(tmp_path, bug_id="BUG-1")
    assert "step_resume_count" not in attempt.result.final_state


def test_records_are_purged_when_the_run_completes(tmp_path, store):
    attempt = _run(tmp_path, bug_id="BUG-1")
    assert attempt.result.outcome == "fixed"
    assert store.load("BUG-1", "agent-0") is None, "a finished run must leave no record"
    assert store.load("BUG-1", "env") is None


def test_records_are_purged_even_when_the_run_errors(tmp_path, store):
    """"Finished in this process" — however it finished — means clean up."""
    attempt = _run(tmp_path, bug_id="BUG-1", scripts={"": [("boom", "exit 1")] * 3})
    # Nothing submits and the script runs out, so mini reports an error.
    assert attempt.result.outcome == "error"
    assert store.load("BUG-1", "agent-0") is None


def test_records_survive_a_kill(tmp_path, store):
    """The mirror image, and the whole point: an abnormal exit keeps them.

    An earlier draft cleaned up in a `finally`, which also fires while the kill
    is propagating — deleting the record exactly when it was about to be needed.
    """
    attempt = _run(tmp_path, bug_id="BUG-1", die_before_query=3)
    assert isinstance(attempt.raised, _Killed)
    assert store.load("BUG-1", "agent-0") is not None


def _self_built_env(monkeypatch, tmp_path):
    """Make fix() build its own environment, so it owns disposal of it."""
    env = _TestEnv(cwd=_cwd(tmp_path))
    monkeypatch.setattr(M.MiniSweAgent, "_build_env", lambda self, instance: env)
    return env


def test_self_built_environment_is_released_on_completion(tmp_path, monkeypatch):
    env = _self_built_env(monkeypatch, tmp_path)
    model = _CacheLikeModel(scripts={"": _SCRIPT})
    agent = M.MiniSweAgent(model=model, mini_config={"agent": dict(_AGENT_CFG)})
    agent.fix(BugInput(bug_id="BUG-1", provider=None,
                       metadata={"swebench_instance": _INSTANCE}))
    assert env.released is True


def test_self_built_environment_survives_a_kill(tmp_path, monkeypatch):
    """Releasing here would destroy the container the restart needs."""
    env = _self_built_env(monkeypatch, tmp_path)
    model = _CacheLikeModel(scripts={"": _SCRIPT}, die_before_query=3)
    agent = M.MiniSweAgent(model=model, mini_config={"agent": dict(_AGENT_CFG)})
    with pytest.raises(_Killed):
        agent.fix(BugInput(bug_id="BUG-1", provider=None,
                           metadata={"swebench_instance": _INSTANCE}))
    assert env.released is False


def test_injected_environments_are_never_released(tmp_path):
    """fix()'s contract: it only disposes of environments it built itself."""
    env = _TestEnv(cwd=_cwd(tmp_path))
    agent = M.MiniSweAgent(model=_CacheLikeModel(scripts={"": _SCRIPT}), env=env,
                           mini_config={"agent": dict(_AGENT_CFG)})
    agent.fix(BugInput(bug_id="BUG-1", provider=None,
                       metadata={"swebench_instance": _INSTANCE}))
    assert env.released is False


# ── U3: done short-circuit ───────────────────────────────────────────────────

def test_finished_record_is_replayed_without_any_llm_call(tmp_path, store):
    """The mini run finished, but the worker died in a later graph node."""
    reference = _run(tmp_path, bug_id="REF")

    # A completed run purges its own records, so rebuild the terminal state as
    # a restarted worker would find it: same key, status=done.
    import agents.mini_resume as R
    real_bind = R.bind_resume

    def _seed_then_bind(agent, *, store, run_key, name, fingerprint, env):
        store.save(run_key, name, {
            "fingerprint": fingerprint, "status": "done", "step": 5, "env_seq": 5,
            "n_calls": 5, "cost": 5.0, "elapsed_s": 1.0, "resume_count": 0,
            "replayed_command_count": 0, "extra_template_vars": {},
            "messages": reference.agents[0].messages,
        })
        real_bind(agent, store=store, run_key=run_key, name=name,
                  fingerprint=fingerprint, env=env)

    R.bind_resume = _seed_then_bind
    try:
        attempt = _run(tmp_path, bug_id="BUG-1")
    finally:
        R.bind_resume = real_bind

    assert attempt.model.queries == 0, "a finished record must not cost a single LLM call"
    assert attempt.result.outcome == "fixed"
    assert (attempt.result.final_state["model_patch"]
            == reference.result.final_state["model_patch"])


# ── U9: the marker command ───────────────────────────────────────────────────

def test_marker_command_shape():
    cmd = marker_command(7, "/dev/shm/.sdlcma_step", "pytest -q")
    assert cmd == "{ printf '%s' 7 > /dev/shm/.sdlcma_step; } 2>/dev/null; pytest -q"


def test_marker_command_suppresses_errors_from_the_redirection_itself(tmp_path):
    """The group's `2>/dev/null` must cover the redirection, not just printf.

    A failing redirect is reported by the shell before printf's own stderr
    handling exists. docker exec merges stderr into stdout here, so that text
    would land in the observation and change the gateway cache key — forking
    the trajectory of every replayed run.
    """
    import subprocess
    wrapped = marker_command(1, "/definitely/not/writable/x", "echo hello")
    out = subprocess.run(["bash", "-lc", wrapped], text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert out.stdout == "hello\n", f"marker leaked into the observation: {out.stdout!r}"


def test_marker_command_never_blocks_the_real_command():
    assert "&&" not in marker_command(1, "/dev/shm/.sdlcma_step", "echo hi")


def test_marker_command_preserves_exit_code_and_first_line(tmp_path):
    """Run it for real: the returncode is the command's and stdout is untouched.

    The first line matters because mini's submission protocol reads it — one
    stray byte from the marker would break every submission.
    """
    import subprocess
    marker = tmp_path / "marker"
    for command, expected_rc in (("echo hello", 0), ("exit 3", 3)):
        wrapped = marker_command(11, str(marker), command)
        out = subprocess.run(["bash", "-lc", wrapped], capture_output=True, text=True)
        assert out.returncode == expected_rc
        assert out.stdout == ("hello\n" if expected_rc == 0 else "")
        assert marker.read_text() == "11"


def test_marker_command_tolerates_an_empty_command(tmp_path):
    import subprocess
    wrapped = marker_command(1, str(tmp_path / "m"), "")
    out = subprocess.run(["bash", "-lc", wrapped], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout == ""


# ── U10: reconciliation ──────────────────────────────────────────────────────

def test_kill_during_a_command_is_reported_as_ambiguous(tmp_path, store):
    """The designed half-step: command #4 started, nothing recorded it."""
    first = _run(tmp_path, bug_id="BUG-1", die_at_command=4)
    assert isinstance(first.raised, _Killed)

    # The partial iteration must NOT be recorded — its assistant message has no
    # observation yet, and resuming into that would stack two assistant turns.
    rec = store.load("BUG-1", "agent-0")
    assert (rec["step"], rec["env_seq"]) == (3, 3)
    assert rec["messages"][-1]["role"] != "assistant"

    assert first.env.reconcile(3) == AMBIGUOUS   # marker sits at 4

    second = _run(tmp_path, bug_id="BUG-1", env=first.env)
    assert second.result.outcome == "fixed"
    # The previously invisible risk, now a number.
    assert second.result.final_state["step_replayed_command_count"] == 1


def test_a_multi_command_step_is_ambiguous_and_counts_every_pending_command(tmp_path, store):
    """One iteration can issue SEVERAL commands, and the gap must not be fatal.

    mini's `execute_actions()` runs every action in an assistant message, and
    models routinely emit two bash calls at once (~25% of steps, measured on
    ls4900). A kill after the first of them leaves the marker two ahead of the
    record. The original rule called any gap > 1 "wrong container", purged the
    record and aborted — losing a recoverable run (L2c, astropy-14309).
    """
    first = _run(tmp_path, bug_id="BUG-1", die_at_command=4)
    assert isinstance(first.raised, _Killed)
    rec = store.load("BUG-1", "agent-0")
    assert (rec["step"], rec["env_seq"]) == (3, 3)

    first.env._marker = 5                      # that iteration issued #4 AND #5
    assert first.env.reconcile(3) == AMBIGUOUS
    assert first.env.pending_commands == 2

    second = _run(tmp_path, bug_id="BUG-1", env=first.env)
    assert second.result.outcome == "fixed"
    assert second.result.final_state["step_replayed_command_count"] == 2


def test_a_missing_marker_is_still_a_mismatch():
    """Widening ambiguity must not blind the one real identity signal: a
    container we attached to that ran commands but has no marker file."""
    from agents import mini_resume_env as mre

    env = mre.ResumableDockerEnvironment.__new__(mre.ResumableDockerEnvironment)
    env._attached, env._marker_enabled = True, True
    env._read_marker = lambda: None
    assert env.reconcile(3) == MISMATCH
    assert env.reconcile(0) == CLEAN           # nothing ran; nothing to explain


def test_clean_kill_reports_no_replayed_command(tmp_path):
    """Reconciliation must discriminate, not always answer "ambiguous"."""
    first = _run(tmp_path, bug_id="BUG-1", die_before_query=4)
    assert first.env.reconcile(3) == CLEAN
    second = _run(tmp_path, bug_id="BUG-1", env=first.env)
    assert second.result.final_state["step_replayed_command_count"] == 0


def test_ambiguous_restart_policy_discards_the_run(tmp_path, monkeypatch, store):
    monkeypatch.setenv("BF_STEP_ON_AMBIGUOUS", "restart")
    first = _run(tmp_path, bug_id="BUG-1", die_at_command=4)

    second = _run(tmp_path, bug_id="BUG-1", env=first.env)
    # StepResumeAborted surfaces as an ordinary failed run (mini's own handler
    # converts it), which routes to handle_failure and leaves a clean slate.
    assert second.result.outcome == "error"
    assert "may already have executed" in second.result.error
    assert store.load("BUG-1", "agent-0") is None


def test_bad_ambiguous_policy_is_fatal(tmp_path, monkeypatch):
    """Fail at startup, not deep inside the loop.

    Inside the loop the resume hook's own exception guard would swallow it and
    degrade to "no resume" — the silent-misconfiguration failure this project
    keeps paying for.
    """
    monkeypatch.setenv("BF_STEP_ON_AMBIGUOUS", "ignore")
    attempt = _run(tmp_path, bug_id="BUG-1")
    assert isinstance(attempt.raised, ValueError)
    assert attempt.model.queries == 0, "must fail before spending anything"


def test_marker_far_ahead_of_the_record_resumes_instead_of_aborting(tmp_path, store):
    """A marker ahead by more than one no longer condemns the run.

    It used to: "commands nobody recorded ⇒ not our container ⇒ purge + abort".
    Identity is the fingerprint's job (and a foreign container has no marker
    file at all — see test_a_missing_marker_is_still_a_mismatch); a large gap
    just means the un-checkpointed iteration issued several commands, which
    mini does routinely. Aborting here threw away a recoverable run in L2c.
    """
    first = _run(tmp_path, bug_id="BUG-1", die_before_query=4)
    first.env._marker = 99

    second = _run(tmp_path, bug_id="BUG-1", env=first.env)
    assert second.result.outcome == "fixed"
    assert second.result.final_state["step_resume_count"] == 1
    assert second.result.final_state["step_replayed_command_count"] == 96


def test_records_of_a_different_run_are_ignored(tmp_path, store):
    """Same key, different task ⇒ the record must not be spliced in."""
    first = _run(tmp_path, bug_id="BUG-1", die_before_query=4)
    rec = store.load("BUG-1", "agent-0")
    rec["fingerprint"] = "not-the-same-run"
    store.save("BUG-1", "agent-0", rec)

    second = _run(tmp_path, bug_id="BUG-1", env=first.env)
    assert second.result.outcome == "fixed"
    assert second.model.queries == 5, "should have started over, not resumed"


# ── construction failures must not be masked ─────────────────────────────────

def test_cleanup_survives_a_half_constructed_environment():
    """__del__ fires even when __init__ raised, and must not add noise.

    An AttributeError from cleanup() buries the real error under "Exception
    ignored in __del__" — which is exactly how a missing `image` field
    presented in the first live ver99 run.
    """
    from agents.mini_resume_env import ResumableDockerEnvironment

    broken = ResumableDockerEnvironment.__new__(ResumableDockerEnvironment)
    broken.cleanup()          # no `config`, no `_store`, no `container_id`
    broken.__del__()


# ── U5: fail-fast / forgiveness ──────────────────────────────────────────────

def test_storage_failure_degrades_but_does_not_break_the_run(tmp_path, monkeypatch):
    """Runtime-forgiving: a broken store costs a re-run, never the run."""
    from services import step_checkpoint as SC

    class _BrokenStore(SC.FileStore):
        def save(self, *a, **kw):
            raise OSError("disk full")

    monkeypatch.setattr(SC, "FileStore", _BrokenStore)
    attempt = _run(tmp_path, bug_id="BUG-1")
    assert attempt.result.outcome == "fixed"


def test_resume_requires_the_vendored_loop(monkeypatch):
    """Upstream has no seam, so the combination is fatal rather than silent."""
    monkeypatch.setenv("MINI_IMPL", "upstream")
    monkeypatch.setenv("BF_STEP_CHECKPOINT", "file")
    with pytest.raises(ValueError, match="MINI_IMPL"):
        M._default_agent_class()


def test_resume_selects_the_resumable_classes():
    assert M._default_agent_class().__name__ == "ResumableAgent"
    assert M._default_environment_class() == M._RESUMABLE_DOCKER_ENV_CLASS


def test_feature_off_leaves_class_selection_untouched(monkeypatch):
    """The additive guarantee, at the seam that decides what actually runs."""
    monkeypatch.setenv("BF_STEP_CHECKPOINT", "none")
    monkeypatch.setenv("MINI_IMPL", "vendored")
    assert M._default_agent_class().__module__ == "agents.vendor.mini.agent"
    assert M._default_environment_class() == M._VENDORED_DOCKER_ENV_CLASS

    monkeypatch.delenv("MINI_IMPL")
    assert M._default_agent_class().__module__ == "minisweagent.agents.default"
    assert M._default_environment_class() == "docker"


# ── U6: staged modes ─────────────────────────────────────────────────────────

_PHASED_SCRIPTS = {
    # Keyed on a phrase that really occurs in that phase's system prompt.
    # "" is the fallback and MUST stay last: it is a substring of everything.
    "investigating a bug": [
        ("investigating", "echo investigating"),
        ("hand off", "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\nROOT_CAUSE: off by one\\n'"),
    ],
    "": [
        ("fixing", "echo fixing"),
        ("verify", "echo verify"),
        ("submit patch", _SUBMIT),
    ],
}


def test_staged_mode_binds_one_record_per_agent(tmp_path, store):
    """Modes 1/3 build several agents on one environment; each needs its own."""
    first = _run(tmp_path, bug_id="BUG-1", mode=1, scripts=_PHASED_SCRIPTS,
                 die_before_query=4)
    assert isinstance(first.raised, _Killed)

    # Phase 1 finished (2 calls, ending in its handoff); phase 2 was one
    # iteration in when the worker died.
    assert store.load("BUG-1", "agent-0")["status"] == "done"
    assert store.load("BUG-1", "agent-1")["status"] == "running"


def test_staged_mode_resume_replays_finished_phases_for_free(tmp_path):
    first = _run(tmp_path, bug_id="BUG-1", mode=1, scripts=_PHASED_SCRIPTS,
                 die_before_query=4)
    second = _run(tmp_path, bug_id="BUG-1", mode=1, scripts=_PHASED_SCRIPTS,
                  env=first.env)

    assert second.result.outcome == "fixed"
    # Phase 1's 2 calls come from its record and phase 2's first is restored,
    # so only calls 4 and 5 are actually re-issued.
    assert second.model.queries == 2
    assert second.result.iterations == 5
    assert second.result.final_state["phase1_calls"] == 2
