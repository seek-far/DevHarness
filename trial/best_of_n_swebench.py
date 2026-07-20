#!/usr/bin/env python3
"""Blind best-of-N selector for SWE-bench mini-swe-agent trajectories.

Default use compares the SDLCMA SWE-bench run against the direct
mini-swe-agent run and asks an LLM to choose the trajectory/patch most likely
to be resolved for instances where the runs disagree.

The LLM never receives the official resolved labels.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_ENV_FILE = Path("settings/worker_local_multi_process.env")
DEFAULT_RUNS = (
    ("sdlcma", Path("/mnt/d/minus/sdlcma/evaluation/runs/wf0_0_100")),
    ("mini", Path("/mnt/d/PL/mini-swe-agent/verified_1-100.BASE_ENV_full")),
)
SDLCMA_FALLBACK = Path("/mnt/d/minus/sdlcma/wf0_0_100")
REPORT_KEYS = {
    "resolved_ids",
    "unresolved_ids",
    "submitted_ids",
    "resolved_instances",
    "submitted_instances",
}
IMPORTANT_RE = re.compile(
    r"(FAIL|FAILED|ERROR|Traceback|AssertionError|ImportError|ModuleNotFoundError|"
    r"passed|collected|COMPLETE_TASK|git diff|diff --git|patch\.txt|submit)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Run:
    name: str
    root: Path
    report_path: Path
    resolved_ids: set[str]
    unresolved_ids: set[str]
    submitted_ids: set[str]
    instance_ids: set[str]


@dataclass(frozen=True)
class Candidate:
    label: str
    run: Run
    instance_id: str
    resolved: bool
    trajectory_path: Path
    trajectory_chars: int
    prompt_chars: int
    compressed: bool
    compression_method: str
    complete: bool
    prompt_text: str


def parse_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("'").strip('"')
        env[key.strip()] = value
    return env


def truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit < 1000:
        return text[:limit]
    head = limit // 2
    tail = limit - head - 80
    return (
        text[:head]
        + f"\n\n[... omitted {len(text) - head - tail} chars from the middle ...]\n\n"
        + text[-tail:]
    )


def short_json(value: Any, limit: int) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except TypeError:
        text = str(value)
    return truncate_middle(text, limit)


def resolve_default_path(name: str, path: Path) -> Path:
    if path.exists():
        return path
    if name == "sdlcma" and path == DEFAULT_RUNS[0][1] and SDLCMA_FALLBACK.exists():
        return SDLCMA_FALLBACK
    return path


def find_report(root: Path) -> Path:
    candidates: list[Path] = []
    for path in root.glob("*.json"):
        if path.name in {"preds.json", "summary.json", "results.json"}:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and REPORT_KEYS.issubset(data.keys()):
            candidates.append(path)
    if len(candidates) != 1:
        names = ", ".join(p.name for p in candidates) or "none"
        raise FileNotFoundError(f"{root}: expected one SWE-bench report JSON, found {names}")
    return candidates[0]


def load_run(name: str, root: Path) -> Run:
    root = resolve_default_path(name, root)
    if not root.exists():
        raise FileNotFoundError(f"run path does not exist: {root}")
    report_path = find_report(root)
    data = json.loads(report_path.read_text(encoding="utf-8"))
    instance_ids = set()
    for key in (
        "submitted_ids",
        "completed_ids",
        "resolved_ids",
        "unresolved_ids",
        "empty_patch_ids",
        "error_ids",
        "incomplete_ids",
    ):
        instance_ids.update(data.get(key) or [])
    return Run(
        name=name,
        root=root,
        report_path=report_path,
        resolved_ids=set(data.get("resolved_ids") or []),
        unresolved_ids=set(data.get("unresolved_ids") or []),
        submitted_ids=set(data.get("submitted_ids") or data.get("completed_ids") or []),
        instance_ids=instance_ids,
    )


def trajectory_path(run: Run, instance_id: str) -> Path:
    candidates = [
        run.root / "trajectories" / f"{instance_id}.traj.json",
        run.root / instance_id / f"{instance_id}.traj.json",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(f"{run.name}: no trajectory found for {instance_id}")


def trajectory_is_complete(path: Path) -> tuple[bool, int]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in raw, len(raw)
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    exit_status = info.get("exit_status")
    submission = info.get("submission")
    complete = (
        exit_status == "Submitted"
        or "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in raw
        or (isinstance(submission, str) and bool(submission.strip()))
    )
    return complete, len(raw)


def final_patch_from_traj(data: dict[str, Any]) -> str:
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    patch = info.get("submission")
    return patch if isinstance(patch, str) else ""


def trajectory_message_entry(
    msg: Any,
    idx: int,
    *,
    content_limit: int,
    raw_output_limit: int,
) -> dict[str, Any] | None:
    if not isinstance(msg, dict):
        return None
    extra = msg.get("extra") if isinstance(msg.get("extra"), dict) else {}
    entry: dict[str, Any] = {
        "index": idx,
        "role": msg.get("role"),
    }
    content = msg.get("content")
    if isinstance(content, str) and content:
        entry["content"] = truncate_middle(content, content_limit)
    actions = extra.get("actions")
    if isinstance(actions, list) and actions:
        entry["actions"] = actions[:4]
    for key in ("returncode", "exception_info"):
        if key in extra:
            entry[key] = extra.get(key)
    raw_output = extra.get("raw_output")
    if isinstance(raw_output, str) and raw_output:
        entry["raw_output"] = truncate_middle(raw_output, raw_output_limit)
    return entry


def render_compressed_trajectory(
    *,
    raw_chars: int,
    threshold: int,
    header: dict[str, Any],
    message_count: int,
    selected: dict[int, dict[str, Any]],
    max_chars: int,
    raw_context: dict[str, str] | None = None,
) -> str:
    compressed = {
        "compression_note": (
            f"Original trajectory was {raw_chars} chars, above threshold {threshold}. "
            "Kept run metadata, final patch, first messages, last messages, messages "
            "near errors/tests/submission/diff evidence, filled remaining budget "
            "with chronological context, and finally added raw excerpts when budget remained."
        ),
        "info": header,
        "message_count": message_count,
        "selected_messages": [selected[idx] for idx in sorted(selected)],
    }
    if raw_context:
        compressed["raw_context_excerpts"] = raw_context
    return short_json(compressed, max_chars)


def compress_trajectory_rule(path: Path, *, threshold: int, max_chars: int) -> tuple[str, bool, int, str]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    if len(raw) <= threshold:
        return raw, False, len(raw), "none"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return truncate_middle(raw, max_chars), True, len(raw), "rule"

    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    config = info.get("config") if isinstance(info.get("config"), dict) else {}
    env = ((config.get("environment") or {}).get("env") or {}) if isinstance(config, dict) else {}
    model_stats = info.get("model_stats") if isinstance(info.get("model_stats"), dict) else {}
    messages = data.get("messages") if isinstance(data.get("messages"), list) else []

    header = {
        "trajectory_file": path.name,
        "trajectory_format": data.get("trajectory_format"),
        "mini_version": info.get("mini_version"),
        "exit_status": info.get("exit_status"),
        "model_stats": model_stats,
        "environment_env_keys": sorted(env.keys()) if isinstance(env, dict) else [],
        "final_patch": final_patch_from_traj(data),
    }

    selected: dict[int, dict[str, Any]] = {}
    keep_indices = set(range(min(8, len(messages))))
    keep_indices.update(range(max(0, len(messages) - 28), len(messages)))
    for idx, msg in enumerate(messages):
        blob = json.dumps(msg, ensure_ascii=False)
        if IMPORTANT_RE.search(blob):
            keep_indices.add(idx)
            if idx > 0:
                keep_indices.add(idx - 1)
            if idx + 1 < len(messages):
                keep_indices.add(idx + 1)

    for idx in sorted(keep_indices):
        entry = trajectory_message_entry(
            messages[idx],
            idx,
            content_limit=2500,
            raw_output_limit=4000,
        )
        if entry is not None:
            selected[idx] = entry

    text = render_compressed_trajectory(
        raw_chars=len(raw),
        threshold=threshold,
        header=header,
        message_count=len(messages),
        selected=selected,
        max_chars=max_chars,
    )

    # The initial evidence slice is often far below the requested cap. Treat
    # max_chars as a usable context budget by filling it with chronological
    # low-priority messages, while keeping the final prompt under the cap.
    if len(text) < int(max_chars * 0.9):
        for idx, msg in enumerate(messages):
            if idx in selected:
                continue
            entry = trajectory_message_entry(
                msg,
                idx,
                content_limit=1800,
                raw_output_limit=3000,
            )
            if entry is None:
                continue
            selected[idx] = entry
            trial_text = render_compressed_trajectory(
                raw_chars=len(raw),
                threshold=threshold,
                header=header,
                message_count=len(messages),
                selected=selected,
                max_chars=max_chars,
            )
            if len(trial_text) > max_chars:
                selected.pop(idx, None)
                continue
            text = trial_text
            if len(text) >= int(max_chars * 0.95):
                break

    if len(text) < int(max_chars * 0.9):
        remaining = max_chars - len(text) - 4000
        if remaining > 3000:
            chunk = max(1000, remaining // 3)
            raw_context = {
                "head": raw[:chunk],
                "middle": raw[max(0, len(raw) // 2 - chunk // 2): len(raw) // 2 + chunk // 2],
                "tail": raw[-chunk:],
            }
            text = render_compressed_trajectory(
                raw_chars=len(raw),
                threshold=threshold,
                header=header,
                message_count=len(messages),
                selected=selected,
                max_chars=max_chars,
                raw_context=raw_context,
            )

    return text, True, len(raw), "rule"


def build_compression_prompt(
    *,
    instance_id: str,
    run_name: str,
    trajectory_path: Path,
    source_text: str,
    source_chars: int,
    target_chars: int,
) -> list[dict[str, str]]:
    system = (
        "You compress SWE-bench agent trajectories for a later blind evaluator. "
        "Do not judge whether the patch passes. Preserve evidence needed for a "
        "different evaluator to decide correctness."
    )
    user = (
        f"Instance: {instance_id}\n"
        f"Run label: {run_name}\n"
        f"Trajectory path: {trajectory_path}\n"
        f"Input chars: {source_chars}\n"
        f"Target output budget: {target_chars} chars\n\n"
        "Compress the trajectory into a structured evidence packet. IMPORTANT: "
        "do not make this a short abstract. Use as much of the target budget as "
        "is useful; aim for 70%-95% of the budget when the input contains enough "
        "material. Prefer preserving concrete evidence, command snippets, test "
        "outputs, traceback excerpts, and patch context over high-level prose.\n\n"
        "Include:\n"
        "- final patch, preferably verbatim\n"
        "- root-cause hypothesis and how it changed\n"
        "- important commands run and their outcomes\n"
        "- tests run, failures, passes, crashes, or missing environment symptoms\n"
        "- any evidence of overfitting, wrong file, broad/unrelated edit, or regression risk\n"
        "- final submission status\n\n"
        "Do not mention or infer the official SWE-bench resolved label. "
        "Return only the compressed trajectory summary.\n\n"
        "TRAJECTORY INPUT:\n"
        f"{source_text}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def compress_trajectory_llm(
    path: Path,
    *,
    instance_id: str,
    run_name: str,
    threshold: int,
    max_chars: int,
    env: dict[str, str],
    model: str,
    timeout: float,
) -> tuple[str, bool, int, str]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    if len(raw) <= threshold:
        return raw, False, len(raw), "none"

    # Keep the rule compressor available as a deterministic pre-filter so the
    # compression LLM never receives more than max_chars of source trajectory.
    source_text = raw
    if len(source_text) > max_chars:
        source_text, _, _, _ = compress_trajectory_rule(
            path,
            threshold=0,
            max_chars=max_chars,
        )

    messages = build_compression_prompt(
        instance_id=instance_id,
        run_name=run_name,
        trajectory_path=path,
        source_text=source_text,
        source_chars=len(source_text),
        target_chars=max_chars,
    )
    compressed = call_llm(messages, env=env, model=model, timeout=timeout).strip()
    if not compressed:
        raise RuntimeError("compression LLM returned empty content")
    return truncate_middle(compressed, max_chars), True, len(raw), "llm"


def compress_trajectory(
    path: Path,
    *,
    instance_id: str,
    run_name: str,
    threshold: int,
    max_chars: int,
    mode: str,
    env: dict[str, str],
    model: str,
    timeout: float,
) -> tuple[str, bool, int, str]:
    if mode == "none":
        raw = path.read_text(encoding="utf-8", errors="replace")
        return raw, False, len(raw), "none"
    if mode == "rule":
        return compress_trajectory_rule(path, threshold=threshold, max_chars=max_chars)
    if mode != "llm":
        raise ValueError(f"unknown compression mode: {mode}")
    try:
        return compress_trajectory_llm(
            path,
            instance_id=instance_id,
            run_name=run_name,
            threshold=threshold,
            max_chars=max_chars,
            env=env,
            model=model,
            timeout=timeout,
        )
    except Exception:
        text, compressed, raw_chars, _ = compress_trajectory_rule(
            path,
            threshold=threshold,
            max_chars=max_chars,
        )
        return text, compressed, raw_chars, "rule_fallback"


def build_prompt(instance_id: str, candidates: list[Candidate]) -> list[dict[str, str]]:
    labels = ", ".join(c.label for c in candidates)
    system = (
        "You are an expert SWE-bench evaluator. You will compare anonymized "
        "agent trajectories for the same SWE-bench instance. You do not know "
        "which candidate resolved the official hidden tests. Choose the single "
        "candidate whose final patch is most likely correct and non-regressing."
    )
    user_parts = [
        f"Instance: {instance_id}",
        "",
        "Compare the candidates below. Use evidence from the trajectory: root-cause analysis, "
        "commands run, test output, final patch minimality, and signs of broken or overfit work.",
        "",
        f"Return only JSON with this shape: "
        f'{{"choice":"<one of {labels}>","confidence":"low|medium|high","rationale":"short reason"}}',
    ]
    for cand in candidates:
        user_parts.extend(
            [
                "",
                f"===== Candidate {cand.label} =====",
                f"Trajectory was compressed: {cand.compressed}",
                f"Trajectory chars before prompt processing: {cand.trajectory_chars}",
                cand.prompt_text,
            ]
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


def parse_choice(raw: str, labels: set[str]) -> tuple[str | None, dict[str, Any]]:
    text = raw.strip()
    data: dict[str, Any] = {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                data = {}
    choice = data.get("choice") if isinstance(data, dict) else None
    if isinstance(choice, str):
        choice = choice.strip().upper()
    if choice not in labels:
        upper = text.upper()
        hits = [label for label in labels if re.search(rf"\b{re.escape(label)}\b", upper)]
        choice = hits[0] if len(hits) == 1 else None
    return choice, data


def normalize_openai_model_name(model: str) -> str:
    """Convert litellm-style provider/model names for direct OpenAI-compatible APIs."""
    if model.startswith("deepseek/"):
        return model.split("/", 1)[1]
    return model


def call_llm(
    messages: list[dict[str, str]],
    *,
    env: dict[str, str],
    model: str,
    timeout: float,
) -> str:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai package is required; install project worker requirements") from exc

    client = OpenAI(
        api_key=env.get("LLM_API_KEY") or os.environ.get("LLM_API_KEY") or "EMPTY",
        base_url=env.get("LLM_API_BASE_URL") or os.environ.get("LLM_API_BASE_URL"),
        timeout=timeout,
    )
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0,
    )
    return response.choices[0].message.content or ""


def parse_run_args(values: list[str] | None) -> list[tuple[str, Path]]:
    if not values:
        return list(DEFAULT_RUNS)
    runs: list[tuple[str, Path]] = []
    for value in values:
        if "=" not in value:
            raise ValueError(f"--run must be NAME=PATH, got {value!r}")
        name, path = value.split("=", 1)
        name = name.strip()
        if not name:
            raise ValueError(f"--run has empty NAME: {value!r}")
        runs.append((name, Path(path)))
    if len(runs) < 2:
        raise ValueError("need at least two --run entries")
    return runs


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def process_instance(
    *,
    iid: str,
    ordered_runs: list[Run],
    shuffled_runs: list[Run],
    labels: list[str],
    env: dict[str, str],
    model: str,
    selection_mode: str,
    compression_mode: str,
    compress_threshold: int,
    max_candidate_chars: int,
    timeout: float,
) -> tuple[dict[str, Any], list[str], bool, bool, bool]:
    log_lines: list[str] = []
    candidate_meta: list[dict[str, Any]] = []
    for label, run in zip(labels, shuffled_runs):
        tpath = trajectory_path(run, iid)
        complete, raw_chars = trajectory_is_complete(tpath)
        candidate_meta.append(
            {
                "label": label,
                "run": run,
                "resolved": iid in run.resolved_ids,
                "trajectory_path": tpath,
                "trajectory_chars": raw_chars,
                "complete": complete,
            }
        )

    def base_record() -> dict[str, Any]:
        return {
            "instance_id": iid,
            "selection_mode": selection_mode,
            "candidates": [
                {
                    "label": c["label"],
                    "run": c["run"].name,
                    "resolved": c["resolved"],
                    "complete": c["complete"],
                    "trajectory_path": str(c["trajectory_path"]),
                    "trajectory_chars": c["trajectory_chars"],
                    "prompt_chars": c.get("prompt_chars", 0),
                    "compressed": c.get("compressed", False),
                    "compression_method": c.get("compression_method", "skipped"),
                }
                for c in candidate_meta
            ],
        }

    if selection_mode == "all":
        complete_meta = [c for c in candidate_meta if c["complete"]]
        if len(complete_meta) == 0:
            first_run = ordered_runs[0]
            chosen_meta = next(c for c in candidate_meta if c["run"].name == first_run.name)
            record = base_record()
            record.update(
                {
                    "selection_strategy": "no_complete_first",
                    "choice": chosen_meta["label"],
                    "chosen_run": chosen_meta["run"].name,
                    "chosen_resolved": chosen_meta["resolved"],
                    "correct": bool(chosen_meta["resolved"]),
                }
            )
            log_lines.append(
                f"{iid}: no COMPLETE; chose first run={record['chosen_run']} "
                f"resolved={record['chosen_resolved']} correct={record['correct']}"
            )
            return record, log_lines, True, bool(record["correct"]), False
        if len(complete_meta) == 1:
            chosen_meta = complete_meta[0]
            record = base_record()
            record.update(
                {
                    "selection_strategy": "single_complete",
                    "choice": chosen_meta["label"],
                    "chosen_run": chosen_meta["run"].name,
                    "chosen_resolved": chosen_meta["resolved"],
                    "correct": bool(chosen_meta["resolved"]),
                }
            )
            log_lines.append(
                f"{iid}: one COMPLETE; chose run={record['chosen_run']} "
                f"resolved={record['chosen_resolved']} correct={record['correct']}"
            )
            return record, log_lines, True, bool(record["correct"]), False

    candidates: list[Candidate] = []
    for meta in candidate_meta:
        label = meta["label"]
        run = meta["run"]
        tpath = meta["trajectory_path"]
        prompt_text, compressed, raw_chars, method = compress_trajectory(
            tpath,
            instance_id=iid,
            run_name=run.name,
            threshold=compress_threshold,
            max_chars=max_candidate_chars,
            mode=compression_mode,
            env=env,
            model=model,
            timeout=timeout,
        )
        prompt_chars = len(prompt_text)
        if compressed:
            log_lines.append(
                f"{iid}: compressed candidate {label} ({run.name}) "
                f"method={method} {raw_chars} -> {prompt_chars} chars; path={tpath}"
            )
        meta["prompt_chars"] = prompt_chars
        meta["compressed"] = compressed
        meta["compression_method"] = method
        candidates.append(
            Candidate(
                label=label,
                run=run,
                instance_id=iid,
                resolved=iid in run.resolved_ids,
                trajectory_path=tpath,
                trajectory_chars=raw_chars,
                prompt_chars=prompt_chars,
                compressed=compressed,
                compression_method=method,
                complete=bool(meta["complete"]),
                prompt_text=prompt_text,
            )
        )

    record = base_record()
    try:
        messages = build_prompt(iid, candidates)
        raw = call_llm(messages, env=env, model=model, timeout=timeout)
        choice, parsed = parse_choice(raw, set(labels))
        chosen = next((c for c in candidates if c.label == choice), None)
        record.update(
            {
                "llm_raw_response": raw,
                "llm_parsed": parsed,
                "choice": choice,
                "chosen_run": chosen.run.name if chosen else None,
                "chosen_resolved": chosen.resolved if chosen else None,
                "selection_strategy": "llm",
                "correct": bool(chosen and chosen.resolved),
            }
        )
        log_lines.append(
            f"{iid}: choice={record['choice']} run={record['chosen_run']} "
            f"resolved={record['chosen_resolved']} correct={record['correct']}"
        )
        return record, log_lines, True, bool(record["correct"]), False
    except Exception as exc:
        record.update({"error": f"{type(exc).__name__}: {exc}", "correct": False})
        log_lines.append(f"{iid}: ERROR {record['error']}")
        return record, log_lines, False, False, True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", help="Run to compare, as NAME=PATH. Repeat for best-of-N.")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--output", type=Path, default=None, help="JSONL output path.")
    parser.add_argument("--limit", type=int, default=0, help="Only process the first N selected instances.")
    parser.add_argument(
        "--selection-mode",
        choices=("disagree", "all"),
        default="disagree",
        help="disagree compares only resolved-status disagreements; all evaluates every common instance.",
    )
    parser.add_argument("--seed", type=int, default=0, help="Seed for candidate label shuffling.")
    parser.add_argument("--compress-threshold", type=int, default=350_000)
    parser.add_argument("--max-candidate-chars", type=int, default=350_000)
    parser.add_argument(
        "--compression-mode",
        choices=("llm", "rule", "none"),
        default="rule",
        help="How to compress trajectories above --compress-threshold. Default: rule; llm uses an LLM compressor.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Judge model name. Defaults to LLM_MODEL from env, with direct-API "
            "normalization such as deepseek/deepseek-v4-pro -> deepseek-v4-pro."
        ),
    )
    parser.add_argument("--workers", type=int, default=20, help="Parallel instance workers.")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--dry-run", action="store_true", help="List selected instance metadata without calling the LLM.")
    args = parser.parse_args(argv)

    run_specs = parse_run_args(args.run)
    runs = [load_run(name, root) for name, root in run_specs]
    common_instances = sorted(set.intersection(*(run.instance_ids for run in runs)))
    disagreements = [
        iid for iid in common_instances
        if len({iid in run.resolved_ids for run in runs}) > 1
    ]
    selected_instances = disagreements if args.selection_mode == "disagree" else common_instances
    if args.limit:
        selected_instances = selected_instances[: args.limit]

    print(f"Loaded {len(runs)} runs:")
    for run in runs:
        print(
            f"  {run.name}: root={run.root} report={run.report_path.name} "
            f"resolved={len(run.resolved_ids)} submitted={len(run.submitted_ids)}"
        )
    print(f"Selection mode: {args.selection_mode}")
    print(f"Common instances: {len(common_instances)}")
    print(f"Disagreeing submitted instances: {len(disagreements)}")
    for iid in disagreements:
        statuses = ", ".join(f"{run.name}={'R' if iid in run.resolved_ids else 'U'}" for run in runs)
        print(f"  {iid}: {statuses}")
    print(f"Selected instances to process: {len(selected_instances)}")
    if args.dry_run:
        return 0

    env = parse_dotenv(args.env_file)
    raw_model = args.model or env.get("LLM_MODEL") or os.environ.get("LLM_MODEL") or ""
    model = args.model or normalize_openai_model_name(raw_model)
    print(f"Judge model: {model} (from {raw_model})")
    print(
        f"Compression mode: {args.compression_mode}; "
        f"threshold={args.compress_threshold}; max_candidate_chars={args.max_candidate_chars}"
    )
    print(f"Workers: {args.workers}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or Path("trial") / f"best_of_n_{stamp}.jsonl"
    aggregate_path = output.with_suffix(".summary.json")

    rng = random.Random(args.seed)
    labels = [chr(ord("A") + i) for i in range(len(runs))]
    shuffled_by_iid: dict[str, list[Run]] = {}
    for iid in selected_instances:
        shuffled = list(runs)
        rng.shuffle(shuffled)
        shuffled_by_iid[iid] = shuffled

    correct = 0
    attempted = 0
    errors = 0
    output.parent.mkdir(parents=True, exist_ok=True)

    with output.open("w", encoding="utf-8") as fh:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {
                executor.submit(
                    process_instance,
                    iid=iid,
                    ordered_runs=runs,
                    shuffled_runs=shuffled_by_iid[iid],
                    labels=labels,
                    env=env,
                    model=model,
                    selection_mode=args.selection_mode,
                    compression_mode=args.compression_mode,
                    compress_threshold=args.compress_threshold,
                    max_candidate_chars=args.max_candidate_chars,
                    timeout=args.timeout,
                ): iid
                for iid in selected_instances
            }
            for future in as_completed(futures):
                iid = futures[future]
                try:
                    record, log_lines, did_attempt, was_correct, had_error = future.result()
                except Exception as exc:
                    record = {
                        "instance_id": iid,
                        "error": f"{type(exc).__name__}: {exc}",
                        "correct": False,
                    }
                    log_lines = [f"{iid}: ERROR {record['error']}"]
                    did_attempt = False
                    was_correct = False
                    had_error = True

                for line in log_lines:
                    stream = sys.stderr if ": ERROR " in line else sys.stdout
                    print(line, file=stream)
                if did_attempt:
                    attempted += 1
                if was_correct:
                    correct += 1
                if had_error:
                    errors += 1
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()

    summary = {
        "runs": [
            {
                "name": run.name,
                "root": str(run.root),
                "report_path": str(run.report_path),
                "resolved": len(run.resolved_ids),
                "submitted": len(run.submitted_ids),
            }
            for run in runs
        ],
        "disagreeing_instances": len(disagreements),
        "selected_instances": len(selected_instances),
        "attempted": attempted,
        "errors": errors,
        "correct": correct,
        "accuracy": (correct / attempted) if attempted else None,
        "judge_model": model,
        "selection_mode": args.selection_mode,
        "compression_mode": args.compression_mode,
        "compress_threshold": args.compress_threshold,
        "max_candidate_chars": args.max_candidate_chars,
        "workers": args.workers,
        "output": str(output),
    }
    write_json(aggregate_path, summary)
    print(f"Wrote {output}")
    print(f"Wrote {aggregate_path}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
