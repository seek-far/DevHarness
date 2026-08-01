"""I1-I3 + L1e — container re-attach and the half-step, against real docker.

Opt-in (`BF_TEST_DOCKER=1`), because the default suite must stay dockerless.
Never pulls: the image has to be present already, since one of the hosts these
run on is behind the CN network.

L1e is the one that matters. It kills a real child process with SIGKILL while a
command is running in a real container, and only that shape can demonstrate the
premise the whole feature rests on: `cleanup()` hangs off `__del__`, so nothing
runs on the way out and the container is still there afterwards. An in-process
exception cannot show that — it would run `__del__`.

Run with:
    BF_TEST_DOCKER=1 uv run pytest tests/test_mini_resume_docker.py -v
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "bf_worker"))

pytest.importorskip("minisweagent", reason="upstream mini-swe-agent not installed")

from agents.mini_resume_env import (  # noqa: E402
    AMBIGUOUS, CLEAN, MISMATCH, DEFAULT_MARKER_PATH, ResumableDockerEnvironment,
)
from services.step_checkpoint import FileStore, run_fingerprint  # noqa: E402

# Any small image with bash. Deliberately not pulled — see module docstring.
IMAGE = os.environ.get("BF_TEST_DOCKER_IMAGE") or "redis:latest"


def _docker_available() -> bool:
    try:
        subprocess.run(["docker", "image", "inspect", IMAGE],
                       capture_output=True, timeout=30, check=True)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    os.environ.get("BF_TEST_DOCKER") != "1" or not _docker_available(),
    reason=f"set BF_TEST_DOCKER=1 and have {IMAGE} present locally",
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _running(container_id: str) -> bool:
    out = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", container_id],
                         capture_output=True, text=True)
    return out.stdout.strip() == "true"


def _exec(container_id: str, command: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "exec", container_id, "bash", "-lc", command],
                          capture_output=True, text=True)


@pytest.fixture
def store(tmp_path) -> FileStore:
    return FileStore(tmp_path / "cp")


@pytest.fixture
def reaper():
    """Remove whatever the test started, however the test ended."""
    started: list[str] = []
    yield started
    for cid in started:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)


def _make_env(store, tmp_path, *, key="BUG-1", fingerprint="fp-1") -> ResumableDockerEnvironment:
    return ResumableDockerEnvironment(
        store=store, image=IMAGE, cwd="/", timeout=120,
        sdlcma_resume_key=key, sdlcma_run_fingerprint=fingerprint,
    )


# ── I1: re-attach ────────────────────────────────────────────────────────────

def test_reattaches_to_the_same_container_without_starting_a_new_one(store, tmp_path, reaper):
    env1 = _make_env(store, tmp_path)
    reaper.append(env1.container_id)
    assert env1.attached is False
    env1.execute({"command": "echo hello > /survivor.txt"})

    # Drop the object WITHOUT releasing — the "worker died" shape.
    first_id = env1.container_id
    del env1

    env2 = _make_env(store, tmp_path)
    assert env2.container_id == first_id, "must attach, not start a new container"
    assert env2.attached is True
    assert _exec(first_id, "cat /survivor.txt").stdout.strip() == "hello"


def test_marker_selftest_passes_and_the_marker_advances(store, tmp_path, reaper):
    env = _make_env(store, tmp_path)
    reaper.append(env.container_id)
    assert env._marker_enabled is True, "self-test should pass on a normal image"

    env.execute({"command": "true"})
    env.execute({"command": "true"})
    assert env.seq == 2
    assert env._read_marker() == 2


def test_the_marker_leaves_the_observation_untouched(store, tmp_path, reaper):
    """The property the gateway cache key depends on."""
    env = _make_env(store, tmp_path)
    reaper.append(env.container_id)
    out = env.execute({"command": "echo hello"})
    assert out["output"] == "hello\n"
    assert out["returncode"] == 0
    assert env.execute({"command": "exit 7"})["returncode"] == 7


# ── I2: the container is the ground truth ────────────────────────────────────

def test_a_dead_container_purges_the_whole_run(store, tmp_path, reaper):
    """§5.2: agent records may never outlive the container they describe.

    Resuming a conversation that claims edits into a fresh /testbed is the one
    failure that reports nothing — the model "finishes" and submits an empty
    diff — so the records die with the container, always.
    """
    env1 = _make_env(store, tmp_path)
    first_id = env1.container_id
    store.save("BUG-1", "agent-0", {"fingerprint": "fp-1", "status": "running",
                                    "step": 3, "messages": []})
    del env1
    subprocess.run(["docker", "rm", "-f", first_id], capture_output=True)

    env2 = _make_env(store, tmp_path)
    reaper.append(env2.container_id)
    assert env2.container_id != first_id
    assert env2.attached is False
    assert store.load("BUG-1", "agent-0") is None, "the conversation must have died too"


def test_a_container_from_a_different_run_is_not_reused(store, tmp_path, reaper):
    """Re-running an instance (batch keys on instance_id) must start clean."""
    env1 = _make_env(store, tmp_path, fingerprint="fp-1")
    reaper.append(env1.container_id)
    env1.execute({"command": "touch /from_run_1"})
    del env1

    env2 = _make_env(store, tmp_path, fingerprint="fp-2")
    reaper.append(env2.container_id)
    assert env2.attached is False
    assert _exec(env2.container_id, "test -e /from_run_1").returncode != 0


# ── I2b: the timeout path, against a real container ──────────────────────────

def test_a_timeout_reproduces_upstreams_observation_and_kills_nothing(store, tmp_path, reaper):
    """The branch most likely to change what the model sees — and the one that
    never fired in production.

    Measured across a 100-instance L3 run: zero timeouts, so this path had only
    unit coverage while being exactly the place where an observation can drift
    (upstream embeds the docker exec argv in the exception text, and ours is a
    different argv). Three things have to hold, and only a real container can
    show them: the partial output reaches the model, the shape matches what
    `subprocess.run(timeout=)` produces, and the command inside the container
    **keeps running** — upstream only ever kills the client, and killing the
    process instead would leave /testbed in a state upstream never reaches.
    """
    env = _make_env(store, tmp_path, key="TO-1")
    reaper.append(env.container_id)
    assert env.ledger_enabled, "the ledger is what this test is about"

    out = env.execute({"command": "echo partial; sleep 30; touch /FINISHED"}, timeout=2)

    assert out["returncode"] == -1
    assert "timed out after" in out["exception_info"]
    assert out["extra"]["exception_type"] == "TimeoutExpired"
    assert out["output"] == "partial\n", "the model must still see what was printed"

    # Nothing was killed: the command is still running, and finishes on its own.
    assert _exec(env.container_id, "test -e /FINISHED").returncode != 0
    time.sleep(32)
    assert _exec(env.container_id, "test -e /FINISHED").returncode == 0, \
        "the container-side process must survive a client-side timeout"

    # And the ledger now holds its result, so a resumed run harvests it rather
    # than running it again.
    d = f"{env._ledger_root}/{env.seq}"
    assert _exec(env.container_id, f"cat {d}/rc").stdout.strip() == "0"


def test_a_timed_out_command_is_harvested_not_rerun_after_resume(store, tmp_path, reaper):
    """The pay-off: the same command, re-entered later, comes back from the
    ledger with its real exit code instead of being issued a second time."""
    env = _make_env(store, tmp_path, key="TO-2")
    reaper.append(env.container_id)
    env.execute({"command": "echo one; sleep 6; echo two >> /RAN"}, timeout=1)
    time.sleep(8)

    env2 = _make_env(store, tmp_path, key="TO-2")      # the replacement worker
    assert env2.attached and env2.container_id == env.container_id
    env2.align(0)                                       # resume: same slot again
    out = env2.execute({"command": "echo one; sleep 6; echo two >> /RAN"})

    assert out["returncode"] == 0
    assert out["output"] == "one\n"
    assert _exec(env.container_id, "wc -l < /RAN").stdout.strip() == "1", \
        "harvested, not re-executed"


# ── I3: self-test degradation ────────────────────────────────────────────────

def test_unwritable_marker_path_degrades_detection_only(store, tmp_path, reaper, monkeypatch):
    """A broken diagnostic must not take down the feature it diagnoses."""
    monkeypatch.setenv("BF_STEP_MARKER_PATH", "/proc/definitely/not/writable")
    env = _make_env(store, tmp_path)
    reaper.append(env.container_id)

    assert env._marker_enabled is False
    # Resume itself still works: the container pointer was still recorded.
    assert store.load("BUG-1", "env")["container_id"] == env.container_id
    # And commands still run, unwrapped and unharmed.
    assert env.execute({"command": "echo fine"})["output"] == "fine\n"


# ── L1e: the real kill ───────────────────────────────────────────────────────

_DRIVER = r'''
import os, sys
sys.path.insert(0, {root!r})
sys.path.insert(0, {bf!r})
os.environ["MSWEA_SILENT_STARTUP"] = "1"
os.environ["MINI_IMPL"] = "vendored"
os.environ["BF_STEP_CHECKPOINT"] = "file"
os.environ["BF_STEP_CHECKPOINT_DIR"] = {cpdir!r}

from agents.base import BugInput
from agents.mini_swe_agent import MiniSweAgent
from minisweagent.models.test_models import DeterministicModel, make_output

SCRIPT = [
    ("one", "echo one"),
    ("two", "echo two"),
    ("the long one", "sleep 60; echo ran >> /RAN"),
    ("four", "echo four"),
    ("submit", "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\ndiff --git a/x b/x\n'"),
]


class CacheLike(DeterministicModel):
    """Answer as a function of the conversation, the way a replay cache does."""

    calls = 0

    def query(self, messages, **kwargs):
        CacheLike.calls += 1
        turn = sum(1 for m in messages if m.get("role") == "assistant")
        thought, command = SCRIPT[turn]
        self.config.outputs = [make_output(thought, [{{"command": command}}])]
        self.current_index = -1
        return super().query(messages, **kwargs)


agent = MiniSweAgent(
    model=CacheLike(outputs=[]),
    mini_config={{
        "agent": {{"system_template": "sys", "instance_template": "{{{{task}}}}",
                  "step_limit": 20, "cost_limit": 0}},
        "environment": {{"image": {image!r}, "cwd": "/", "timeout": 300}},
    }},
)
out = agent.fix(BugInput(bug_id="E2E-1", provider=None, metadata={{"swebench_instance": {{
    "instance_id": "e2e__e2e-1", "image_name": {image!r}, "problem_statement": "fix it",
}}}}))
print("OUTCOME", out.outcome, out.iterations, flush=True)
print("QUERIES", CacheLike.calls, flush=True)
print("REPLAYED", (out.final_state or {{}}).get("step_replayed_command_count"), flush=True)
print("RESUMES", (out.final_state or {{}}).get("step_resume_count"), flush=True)
'''


def _driver(tmp_path) -> Path:
    path = tmp_path / "driver.py"
    path.write_text(_DRIVER.format(
        root=str(_ROOT), bf=str(_ROOT / "bf_worker"),
        cpdir=str(tmp_path / "cp"), image=IMAGE,
    ), encoding="utf-8")
    return path


def _wait_for_marker(store, target: int, timeout: float = 120.0):
    """Poll the container's marker until the long command is under way."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = store.load("E2E-1", "env")
        if rec:
            out = _exec(rec["container_id"], f"cat {DEFAULT_MARKER_PATH} 2>/dev/null")
            if out.stdout.strip() == str(target):
                return rec["container_id"]
        time.sleep(0.5)
    pytest.fail(f"marker never reached {target}")


def test_sigkill_mid_command_waits_for_it_instead_of_running_it_again(store, tmp_path, reaper):
    """L1e-w3-a: the case W2 structurally cannot pass.

    A `kill -9` lands in the middle of a 60-second command. W2 could only say
    "something may have run" and re-issue the whole step — two executions and a
    second LLM call. With the ledger the replacement worker finds the command
    still running, waits for it, and harvests the output it produced, having
    asked the model nothing.

    `/RAN` is appended to rather than touched precisely so that "ran twice" is
    a countable fact rather than an invisible one.
    """
    proc = subprocess.Popen([sys.executable, str(_driver(tmp_path))],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        container_id = _wait_for_marker(store, 3)
        reaper.append(container_id)
        # The 60s command is running now. Nothing in the process gets a say.
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()

    # The premise of the entire feature: cleanup hangs off __del__, which
    # SIGKILL never reaches, so the container outlives the worker.
    assert _running(container_id), "the eval container must survive the worker"
    assert _exec(container_id, "test -e /RAN").returncode != 0, \
        "the long command should still have been in flight"

    rec = json.loads((store.root / "E2E-1" / "agent-0.json").read_text())
    assert rec["status"] == "running"
    assert (rec["step"], rec["env_seq"]) == (2, 2)
    assert rec["messages"][-1]["role"] != "assistant", "the prefix stops at the boundary"
    pending = rec["pending"]
    assert pending["message"]["role"] == "assistant", "the half-step is recorded separately"
    assert pending["first_seq"] == 3

    # Second worker, same store: attach, wait out the command it inherited,
    # harvest its output, finish the job.
    out = subprocess.run([sys.executable, str(_driver(tmp_path))],
                         capture_output=True, text=True, timeout=600)
    assert "OUTCOME fixed" in out.stdout, out.stderr[-3000:]

    assert _exec(container_id, "wc -l < /RAN").stdout.strip() == "1", \
        "the long command must have executed EXACTLY once across both workers"
    assert "OUTCOME fixed 5" in out.stdout
    assert "QUERIES 2" in out.stdout, \
        "the interrupted step must not be re-asked — only steps 4 and 5 are new"
    assert "REPLAYED 0" in out.stdout, "nothing was replayed"
    assert "RESUMES 1" in out.stdout


def test_reconcile_discriminates_against_a_real_container_marker(store, tmp_path, reaper):
    """All three verdicts, read from a real marker file rather than a fake.

    Without this, "always answers ambiguous" would pass every other test here —
    a detector that cannot say "clean" measures nothing.
    """
    env1 = _make_env(store, tmp_path, key="R-1")
    reaper.append(env1.container_id)
    env1.execute({"command": "echo one"})
    env1.execute({"command": "echo two"})
    assert env1._read_marker() == 2
    del env1

    env2 = ResumableDockerEnvironment(
        store=store, image=IMAGE, cwd="/", timeout=120,
        sdlcma_resume_key="R-1", sdlcma_run_fingerprint="fp-1",
    )
    assert env2.attached is True
    assert env2.reconcile(2) == CLEAN        # killed at a boundary
    assert env2.reconcile(1) == AMBIGUOUS    # command #2 had started
    assert env2.pending_commands == 1
    # A gap of two is ALSO ambiguous, not "wrong container": one iteration can
    # issue several commands (mini runs every action in a message).
    assert env2.reconcile(0) == AMBIGUOUS
    assert env2.pending_commands == 2
    # MISMATCH is reserved for the one real identity signal — commands ran but
    # the marker file is not there.
    env2._docker("exec", env2.container_id, "sh", "-c", f"rm -f {DEFAULT_MARKER_PATH}")
    assert env2.reconcile(2) == MISMATCH
    env2._docker("exec", env2.container_id, "sh", "-c",
                 f"echo 2 > {DEFAULT_MARKER_PATH}")   # restore for the tail below

    # reconcile() also restores the counter, so marker numbering keeps climbing
    # across restarts instead of silently going back to zero.
    env2.reconcile(2)
    env2.execute({"command": "echo three"})
    assert env2._read_marker() == 3
