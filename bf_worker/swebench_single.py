"""
swebench_single.py — fix ONE SWE-bench instance via the mini-swe-agent substrate.

Standalone-peer entry point (mirrors bf_worker/standalone.py). It is NOT an
orchestrator/spawner path and does NOT invent a new ENV/spawner (invariant #1).
The full contract is in docs/swebench.md.

Vertical slice (Phase 1):

    load instance (HF dataset) → BugInput(metadata=instance)
      → MiniSweAgent.fix()  [docker Environment + mini loop → patch]
      → official swebench harness → resolved bool
      → RunRecord + journal entry

Usage:

    python -m bf_worker.swebench_single \
      --subset verified --split test \
      --instance sympy__sympy-20590 \
      --config configs/swebench/mini.yaml

    # skip grading (just produce the patch + preds.json):
    python -m bf_worker.swebench_single --instance <id> --no-grade

Requirements (the SWE-bench eval extra — see evaluation/requirements-swebench.txt):
docker, `datasets`, `swebench`, and mini-swe-agent's runtime deps (litellm, …).
These deliberately do NOT ship in the worker/gateway/orchestrator images.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

# Same dual-path setup as standalone.py: project root (for `from settings …`)
# and bf_worker/ (for `from agents …`, sibling-style imports).
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

from agents.base import BugInput  # noqa: E402
from agents.mini_swe_agent import MiniSweAgent, build_gateway_model_config  # noqa: E402
from agents.run_record import RunRecord  # noqa: E402
from journal import JournalWriter  # noqa: E402

logger = logging.getLogger(__name__)

# SWE-bench HF dataset short-name → path. Kept inline (not imported from mini's
# run.benchmarks.swebench, whose module-level typer/rich CLI imports we avoid).
DATASET_MAPPING = {
    "full": "princeton-nlp/SWE-Bench",
    "verified": "princeton-nlp/SWE-Bench_Verified",
    "lite": "princeton-nlp/SWE-Bench_Lite",
    "multimodal": "princeton-nlp/SWE-Bench_Multimodal",
    "multilingual": "swe-bench/SWE-Bench_Multilingual",
    "_test": "klieret/swe-bench-dummy-test-dataset",
}


# ── mini config assembly ─────────────────────────────────────────────────────

def build_mini_config(config_specs: list[str], model_override: str | None) -> dict:
    """Merge mini's builtin swebench.yaml + any user configs, then point the
    model at our LLM gateway / OpenAI-compatible backend via worker settings.

    Reuses mini's own config machinery (get_config_from_spec + recursive_merge)
    so behaviour matches upstream mini; only the model endpoint is redirected.
    """
    from minisweagent.config import builtin_config_dir, get_config_from_spec
    from minisweagent.utils.serialize import recursive_merge

    # Always base on mini's builtin swebench.yaml (system/instance templates,
    # cwd=/testbed, submission protocol), then layer user configs on top — so a
    # passed overlay is purely additive rather than replacing mini's defaults.
    default_swebench = builtin_config_dir / "benchmarks" / "swebench.yaml"
    specs = [str(default_swebench), *config_specs]
    config = recursive_merge(*[get_config_from_spec(s) for s in specs])

    # Redirect mini's litellm model at our gateway/backend. worker_cfg is
    # optional — a bare `-c ...=model_name=...` config can drive it directly.
    # Precedence: CLI --model  >  worker settings  >  mini's config default.
    from agents.mini_swe_agent import _litellm_model_name

    worker_cfg = _maybe_worker_cfg()
    model_cfg = dict(config.get("model") or {})
    if worker_cfg is not None:
        model_cfg = build_gateway_model_config(worker_cfg, model_cfg)
    if model_override:  # CLI --model is the last word
        model_cfg["model_name"] = _litellm_model_name(model_override)
    config["model"] = model_cfg
    return config


def _maybe_worker_cfg():
    try:
        from settings import worker_cfg

        return worker_cfg
    except Exception as exc:  # pragma: no cover - settings tree optional here
        logger.warning("worker settings unavailable (%s); model must be set via -c", exc)
        return None


# ── dataset ──────────────────────────────────────────────────────────────────

def load_instance(subset: str, split: str, instance_spec: str) -> dict:
    from datasets import load_dataset

    dataset_path = DATASET_MAPPING.get(subset, subset)
    logger.info("loading dataset %s split %s ...", dataset_path, split)
    instances = {inst["instance_id"]: inst for inst in load_dataset(dataset_path, split=split)}
    if instance_spec.isnumeric():
        instance_spec = sorted(instances.keys())[int(instance_spec)]
    if instance_spec not in instances:
        raise SystemExit(f"instance {instance_spec!r} not in {dataset_path}/{split}")
    return dict(instances[instance_spec])


# ── grading (official SWE-bench harness) ─────────────────────────────────────

def grade_with_harness(
    subset: str,
    split: str,
    instance_ids: list[str],
    preds_path: Path,
    run_id: str,
    workdir: Path,
    grade_workers: int = 1,
) -> dict[str, bool | None]:
    """Run the official SWE-bench harness over one or more predictions and
    return {instance_id: resolved}. On any harness failure every requested id
    maps to None (never a fabricated False — unknown ≠ graded-and-failed).

    Uses the harness CLI (`python -m swebench.harness.run_evaluation`) rather
    than importing it, so the heavy dep stays out of this module's import path
    and a harness-version drift surfaces as a clear subprocess error.
    """
    dataset_name = DATASET_MAPPING.get(subset, subset)
    cmd = [
        sys.executable, "-m", "swebench.harness.run_evaluation",
        "--dataset_name", dataset_name,
        "--split", split,
        "--predictions_path", str(preds_path),
        "--instance_ids", *instance_ids,
        "--run_id", run_id,
        "--max_workers", str(grade_workers),
        "--cache_level", "env",
    ]
    logger.info("grading %d instance(s) via harness (workers=%d)", len(instance_ids), grade_workers)
    proc = subprocess.run(cmd, cwd=str(workdir), text=True, capture_output=True)
    if proc.returncode != 0:
        logger.error("harness failed (rc=%s):\n%s", proc.returncode, proc.stdout[-2000:] + proc.stderr[-2000:])
        return {iid: None for iid in instance_ids}
    # The harness writes <model>.<run_id>.json in workdir with resolved_ids.
    reports = sorted(workdir.glob(f"*.{run_id}.json"), key=lambda p: p.stat().st_mtime)
    if not reports:
        logger.error("harness produced no report matching *.%s.json", run_id)
        return {iid: None for iid in instance_ids}
    try:
        report = json.loads(reports[-1].read_text())
    except Exception as exc:
        logger.error("could not parse harness report %s: %s", reports[-1], exc)
        return {iid: None for iid in instance_ids}
    resolved_ids = set(report.get("resolved_ids", []))
    return {iid: (iid in resolved_ids) for iid in instance_ids}


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fix one SWE-bench instance via the mini-swe-agent substrate.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--subset", default="verified",
                        help="SWE-bench subset (verified/lite/full/…) or dataset path (default: verified).")
    parser.add_argument("--split", default="test", help="Dataset split (default: test).")
    parser.add_argument("--instance", required=True,
                        help="Instance ID (e.g. sympy__sympy-20590) or numeric index into the sorted split.")
    parser.add_argument("--config", action="append", default=[],
                        help="mini config spec (path/filename/key=value); repeatable. "
                             "Default: mini's builtin benchmarks/swebench.yaml.")
    parser.add_argument("--model", default=None, help="Override the model name (else worker settings / config).")
    parser.add_argument("--workflow-mode", type=int, default=0,
                        help=("Agent workflow: 0=mini default single loop, 1=two-phase waterfall, "
                              "2=stage-report single loop, 3=two-phase back-edge, "
                              "4=mode 0 + per-instance background knowledge."))
    parser.add_argument("--output-dir", default=None,
                        help="Where to write preds.json + harness artifacts (default: temp dir).")
    parser.add_argument("--no-grade", action="store_true",
                        help="Skip the official harness grading (produce the patch + preds.json only).")
    parser.add_argument("--bug-id", default=None, help="Override bug_id (default: the instance id).")
    args = parser.parse_args()

    instance = load_instance(args.subset, args.split, args.instance)
    instance_id = instance["instance_id"]
    bug_id = args.bug_id or instance_id

    output_dir = Path(args.output_dir) if args.output_dir else Path(
        __import__("tempfile").mkdtemp(prefix=f"swebench_{instance_id}_")
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    logger.info("instance=%s output_dir=%s", instance_id, output_dir)

    mini_config = build_mini_config(args.config, args.model)
    model_name = (mini_config.get("model") or {}).get("model_name")

    bug_input = BugInput(bug_id=bug_id, provider=None, metadata={"swebench_instance": instance})
    agent = MiniSweAgent(mini_config=mini_config, workflow_mode=args.workflow_mode,
                         trajectory_dir=output_dir / "trajectories",
                         agent_config={"llm_model": model_name, "workflow_mode": args.workflow_mode})

    t0 = time.monotonic()
    fix_output = agent.fix(bug_input)
    elapsed = time.monotonic() - t0
    logger.info("fix outcome=%s iterations=%d elapsed=%.1fs",
                fix_output.outcome, fix_output.iterations, elapsed)

    final_state = dict(fix_output.final_state or {})
    patch = final_state.get("model_patch") or ""

    # preds.json in the SWE-bench harness shape.
    preds_path = output_dir / "preds.json"
    preds_path.write_text(json.dumps({
        instance_id: {
            "model_name_or_path": model_name or "mini_swe_agent",
            "instance_id": instance_id,
            "model_patch": patch,
        }
    }, indent=2), encoding="utf-8")

    resolved: bool | None = None
    if not args.no_grade and patch.strip():
        verdicts = grade_with_harness(
            args.subset, args.split, [instance_id], preds_path,
            run_id=f"sdlcma_{bug_id}", workdir=output_dir,
        )
        resolved = verdicts.get(instance_id)
        final_state["resolved"] = resolved
        final_state["test_passed"] = resolved
    logger.info("resolved=%s", resolved)

    # Journal: build the RunRecord here (resolved is a post-fix grading step,
    # so the entry point — not the agent — owns journaling for this path).
    record = RunRecord.from_outputs(
        agent_name=agent.name,
        bug_id=bug_id,
        outcome=fix_output.outcome,
        error=fix_output.error,
        iterations=fix_output.iterations,
        final_state=final_state,
        elapsed_s=round(elapsed, 3),
        agent_config={"llm_model": model_name, "subset": args.subset, "split": args.split,
                      "workflow_mode": args.workflow_mode},
        llm_model=model_name,
    )
    JournalWriter().write(record, final_state)

    print(f"\ninstance : {instance_id}")
    print(f"outcome  : {fix_output.outcome}")
    print(f"resolved : {resolved}")
    print(f"patch    : {'<empty>' if not patch.strip() else str(len(patch)) + ' chars'}")
    print(f"preds    : {preds_path}")

    if fix_output.outcome == "error":
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [swebench_single %(name)s:%(funcName)s:%(lineno)d] %(message)s",
        stream=sys.stdout,
        force=True,
    )
    main()
