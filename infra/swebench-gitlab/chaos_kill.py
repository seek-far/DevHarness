"""L2c chaos driver: SIGKILL a random subset of ver99 workers mid-ReAct-loop.

Runs alongside `sweep.py --concurrency N` and watches the W2 step-checkpoint
directory (`BF_STEP_CHECKPOINT_DIR`, default `~/.sdlcma/step_checkpoints`), one
sub-directory per in-flight run. For every run it sees appear it:

  * records the run's identity (container_id + image, i.e. which SWE-bench
    instance it is) as soon as `env.json` lands;
  * for a random ~`--rate` subset, waits until the loop reaches a random step
    and then `kill -9`s that worker — the HealthMonitor restarts it and the
    replacement is expected to re-attach to the still-running eval container;
  * **snapshots the evidence BEFORE killing** — container_id, step, env_seq and
    the in-container step marker. A successful resume purges the whole record
    directory when the run finishes, so anything not captured here is gone by
    the time you come to assert on it. This is the difference between L2c and
    the single-instance L2, where you could read the record by hand afterwards;
  * notes when each run's directory disappears (= the purge/release path ran);
  * samples the checkpoint dir's disk usage and the live mini-container count.

Everything lands as JSONL for `verify_l2c.py` to assert against offline. Pass
`--no-kill` for the control arm, where the same telemetry is wanted without any
chaos.

Kill-by-`bug_id` rather than "the newest thing on disk" is the whole point: at
concurrency 15 there is no such thing as *the* current run.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import time
from pathlib import Path

# swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest -> astropy__astropy-12907
_IMG_IID = re.compile(r"sweb\.eval\.[^.]+\.(.+?):")

DEFAULT_CHECKPOINT_DIR = "~/.sdlcma/step_checkpoints"
DEFAULT_MARKER_PATH = "/dev/shm/.sdlcma_step"


def instance_of(image: str | None) -> str | None:
    """Recover the instance_id from the eval image name."""
    if not image:
        return None
    m = _IMG_IID.search(image)
    return m.group(1).replace("_1776_", "__") if m else None


def kill_pattern(run_key: str) -> str:
    """`pkill -f` pattern matching exactly the worker subprocess for one bug_id.

    Mirrors `orchestrator/spawner.py::_start_process`, which execs
    `<python> <repo>/bf_worker/bf_worker.py --bug-id <bug_id>`. Pinned by tests
    so a rename of the worker entry point fails loudly instead of silently
    killing nothing (which would look like "chaos never fired").
    """
    return f"bf_worker.py --bug-id {run_key}"


def plan_for(rng: random.Random, rate: float, min_step: int, max_step: int) -> dict:
    """Draw a run's fate. `doomed` stays None until the instance is known."""
    return {"doomed": None, "roll": rng.random(), "target": rng.randint(min_step, max_step),
            "killed": False, "env_logged": False, "purged": False}


def decide_doom(plan: dict, instance: str | None, rate: float,
                only: set[str] | None) -> bool:
    """Kill this run?  Deferred until `env.json` names the instance.

    `only` restricts chaos to the judgeable subset — the instances that replayed
    100% from cache in the control arms. Killing outside it wastes the kill:
    that instance's trajectory already diverged for cache reasons, so comparing
    it against the control proves nothing either way. At L3 scale (~60 of 100
    instances judgeable) a blind draw would spend 40% of the chaos budget on
    unusable cells.
    """
    if only is not None and (instance is None or instance not in only):
        return False
    return plan["roll"] < rate


def should_fire(agent_doc: dict | None, target: int) -> bool:
    """True once the loop has completed `target` steps."""
    if not agent_doc:
        return False
    try:
        return int(agent_doc.get("step", 0)) >= target
    except (TypeError, ValueError):
        return False


def read_json(p: Path):
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _run(cmd: list[str], timeout: int = 30) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def docker(*args: str) -> str | None:
    return _run(["docker", *args])


def emit(fh, kind: str, **fields) -> None:
    fh.write(json.dumps({"ts": time.time(), "kind": kind, **fields}, ensure_ascii=False) + "\n")
    fh.flush()
    print(f"[{time.strftime('%H:%M:%S')}] {kind} {fields}", flush=True)


def watch(args) -> None:
    rng = random.Random(args.seed)
    rate = 0.0 if args.no_kill else args.rate
    only = None
    if args.only_instances:
        only = {l.strip() for l in Path(os.path.expanduser(args.only_instances)).read_text()
                .splitlines() if l.strip()}
    root = Path(os.path.expanduser(args.dir))
    root.mkdir(parents=True, exist_ok=True)
    stop = Path(os.path.expanduser(args.stop_file))
    if stop.exists():
        stop.unlink()

    seen: dict[str, dict] = {}
    deadline = time.time() + args.duration
    last_sample = 0.0

    with open(os.path.expanduser(args.events), "a") as fh:
        emit(fh, "start", dir=str(root), rate=args.rate, no_kill=args.no_kill,
             min_step=args.min_step, max_step=args.max_step, seed=args.seed)
        while time.time() < deadline and not stop.exists():
            live = {p.name for p in root.iterdir() if p.is_dir()}

            for key in sorted(live - set(seen)):
                plan = plan_for(rng, rate, args.min_step, args.max_step)
                seen[key] = plan
                emit(fh, "observe", run_key=key, target_step=plan["target"])

            for key, plan in seen.items():
                d = root / key
                if not d.exists():
                    if not plan["purged"]:
                        plan["purged"] = True
                        emit(fh, "purge", run_key=key, was_killed=plan["killed"])
                    continue
                plan["purged"] = False

                if not plan["env_logged"]:
                    env = read_json(d / "env.json")
                    if env:
                        plan["env_logged"] = True
                        plan["container_id"] = env.get("container_id")
                        plan["image"] = env.get("image")
                        instance = instance_of(env.get("image"))
                        plan["doomed"] = decide_doom(plan, instance, rate, only)
                        emit(fh, "env", run_key=key, container_id=env.get("container_id"),
                             image=env.get("image"), instance=instance,
                             marker_enabled=env.get("marker_enabled"),
                             doomed=plan["doomed"], target_step=plan["target"])

                if not plan["doomed"] or plan["killed"]:
                    continue
                agent = read_json(d / "agent-0.json")
                if not should_fire(agent, plan["target"]):
                    continue

                # ---- snapshot the evidence BEFORE the kill ------------------
                cid = plan.get("container_id")
                marker = docker("exec", cid, "cat", args.marker_path) if cid else None
                running = docker("inspect", "-f", "{{.State.Running}}", cid) if cid else None
                pkill_ok = subprocess.run(
                    ["pkill", "-9", "-f", kill_pattern(key)]).returncode == 0
                plan["killed"] = True

                # Did this kill actually land INSIDE the loop? If the agent had
                # already finished, it released its records before dying and the
                # replacement has nothing to resume from — a legitimate outcome
                # that must not be scored as "resume failed". Measured, not
                # guessed: look again a moment later.
                time.sleep(args.post_kill_probe)
                record_after = "present" if (d / "agent-0.json").exists() else "gone"
                running_after = docker("inspect", "-f", "{{.State.Running}}", cid) if cid else None
                emit(fh, "kill", run_key=key, instance=instance_of(plan.get("image")),
                     container_id=cid, step=int((agent or {}).get("step", 0)),
                     env_seq=(agent or {}).get("env_seq"), n_calls=(agent or {}).get("n_calls"),
                     marker=marker, container_running_before=running,
                     record_after_kill=record_after, container_running_after=running_after,
                     pkill_ok=pkill_ok, target_step=plan["target"])

            if time.time() - last_sample > args.sample_interval:
                last_sample = time.time()
                du = _run(["du", "-sb", str(root)], timeout=60)
                emit(fh, "disk", bytes=int(du.split()[0]) if du else None,
                     live_runs=len(live),
                     containers=len((docker("ps", "-q", "--filter",
                                            "name=minisweagent") or "").split()))
            time.sleep(args.poll_interval)

        emit(fh, "stop", observed=len(seen),
             killed=sorted(k for k, v in seen.items() if v["killed"]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", default=os.environ.get("BF_STEP_CHECKPOINT_DIR",
                                                    DEFAULT_CHECKPOINT_DIR))
    ap.add_argument("--events", default="~/.sdlcma/w2/chaos_events.jsonl")
    ap.add_argument("--rate", type=float, default=0.34, help="fraction of runs to kill")
    ap.add_argument("--min-step", type=int, default=2)
    ap.add_argument("--max-step", type=int, default=6)
    ap.add_argument("--marker-path", default=DEFAULT_MARKER_PATH)
    ap.add_argument("--seed", type=int, default=20260730)
    ap.add_argument("--duration", type=int, default=7200, help="seconds to stay alive")
    ap.add_argument("--stop-file", default="~/.sdlcma/w2/chaos.stop")
    ap.add_argument("--poll-interval", type=float, default=2.0)
    ap.add_argument("--post-kill-probe", type=float, default=1.5,
                    help="seconds to wait before re-checking whether the record survived")
    ap.add_argument("--sample-interval", type=float, default=30.0)
    ap.add_argument("--no-kill", action="store_true", help="observe only (control arm)")
    ap.add_argument("--only-instances", default="",
                    help="file of instance ids chaos may touch (the judgeable subset)")
    watch(ap.parse_args())


if __name__ == "__main__":
    main()
