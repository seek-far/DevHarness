"""Per-instance LLM-cache hit rate for one or more L2c/L3 arms.

Why per-instance and not overall: an arm's *aggregate* hit rate says nothing
about whether a given instance is comparable across arms. One miss forks that
instance's trajectory, and everything after it in that run is a different
conversation — so the instance stops being evidence for or against anything.
The judgeable population is exactly "the instances that replayed fully in every
arm", and this script computes it.

The gateway logs one line per lookup, tagged with the instance:

    phase_marker phase=cache_lookup mode=cache result=hit key=… bug_id=<instance_id>

Hit rate is a **known-imperfect** criterion for comparability (measured on L3 it
admitted 4 instances that hit 100% while walking a different recorded branch,
and excluded 6 whose only misses were in the recording arm). The sharper one is
**key-sequence identity**, which `--key-sequence` computes: the questions asked,
in order.

    strict     the chaos arm's key sequence equals the control's, exactly.
               This is W2.5's criterion: exactly-once means the resume re-asks
               nothing, so not even a duplicate may appear.
    collapsed  equal after collapsing consecutive duplicates, with the collapse
               count == step_resume_count. That is W2's criterion — at-least-once
               re-sends the interrupted step's question, so one duplicate per
               resume is expected.

usage:
    cache_hitrate.py --arms A A2 C [--clean-subset clean.txt]
    cache_hitrate.py --arms A C --key-sequence [--strict]
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import re
from pathlib import Path

_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*?"
    r"phase=cache_lookup .*?result=(?P<result>\w+).*?key=(?P<key>\S+).*?bug_id=(?P<iid>\S+)")


def parse_log(text: str) -> list[tuple[float, str, str, str]]:
    """[(epoch, result, instance_id, key)] for every cache lookup in the log.

    `key` is appended last so positional readers of the first three fields keep
    working.
    """
    out = []
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        try:
            ts = dt.datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        out.append((ts, m.group("result"), m.group("iid"), m.group("key")))
    return out


def per_instance(lookups, start: float, end: float) -> dict[str, dict]:
    """instance -> {hit, miss, total, rate} inside one arm's window."""
    acc: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for ts, result, iid, *_ in lookups:
        # The log has second resolution; widen by a second on each side rather
        # than silently dropping the first/last call of an arm.
        if start - 1 <= ts <= end + 1:
            acc[iid][result] += 1
    stats = {}
    for iid, c in acc.items():
        total = sum(c.values())
        stats[iid] = {"hit": c["hit"], "miss": c["miss"], "total": total,
                      "rate": (c["hit"] / total) if total else 0.0}
    return stats


def clean_subset(per_arm: dict[str, dict[str, dict]]) -> list[str]:
    """Instances that replayed 100% in EVERY arm — the only comparable ones."""
    arms = list(per_arm)
    if not arms:
        return []
    common = set(per_arm[arms[0]])
    for a in arms[1:]:
        common &= set(per_arm[a])
    return sorted(i for i in common
                  if all(per_arm[a][i]["total"] and per_arm[a][i]["miss"] == 0 for a in arms))


def key_sequences(lookups, start: float, end: float) -> dict[str, list[str]]:
    """instance -> the cache keys it looked up, in order, inside one window."""
    seqs: dict[str, list[str]] = collections.defaultdict(list)
    for ts, _result, iid, key in lookups:
        if start - 1 <= ts <= end + 1:
            seqs[iid].append(key)
    return dict(seqs)


def collapse(seq: list[str]) -> tuple[list[str], int]:
    """Drop consecutive duplicates. Returns (collapsed, how many were dropped).

    A resumed step under W2 re-sends the same question, so its key appears twice
    in a row. Collapsing is therefore exactly the transformation that turns an
    at-least-once trajectory back into the control's — and the count it removes
    is the number of resumes it is explaining.
    """
    out: list[str] = []
    for k in seq:
        if not out or out[-1] != k:
            out.append(k)
    return out, len(seq) - len(out)


def compare_key_sequences(ctrl: dict[str, list[str]], other: dict[str, list[str]],
                          *, strict: bool, only: set[str] | None = None) -> dict:
    """Per-instance verdict on "same questions, same order".

    Instances missing from either arm are `absent`, never `same`: a comparison
    that could not be made must not read as a passing one.
    """
    rows, same, differ, absent = [], [], [], []
    keys = set(ctrl) | set(other)
    if only is not None:
        keys &= only
    for iid in sorted(keys):
        a, b = ctrl.get(iid), other.get(iid)
        if not a or not b:
            absent.append(iid)
            rows.append({"instance": iid, "verdict": "ABSENT", "dups": None})
            continue
        # `dups` is always the chaos arm's own consecutive-duplicate count: the
        # questions it asked twice in a row. Under W2 that is one per resume;
        # under W2.5 it must be zero, which is what --strict then enforces.
        collapsed_b, dups = collapse(b)
        ok = (a == b) if strict else (collapse(a)[0] == collapsed_b)
        (same if ok else differ).append(iid)
        rows.append({"instance": iid, "verdict": "SAME" if ok else "DIFFER",
                     "dups": dups, "len": f"{len(a)}/{len(b)}"})
    return {"rows": rows, "same": same, "differ": differ, "absent": absent}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", nargs="+", required=True)
    ap.add_argument("--w2-dir", default="~/.sdlcma/w2")
    ap.add_argument("--log", default="", nargs="*",
                    help="gateway log(s). Default: llmgw.log AND its rotated "
                         "siblings — the stack rotates on bring-up, so one arm's "
                         "lines routinely live in a different file than the next's")
    ap.add_argument("--clean-subset", default="", help="write the judgeable instance list here")
    ap.add_argument("--per-instance", action="store_true", help="print every instance, not just misses")
    ap.add_argument("--key-sequence", action="store_true",
                    help="compare every arm's key sequence against the FIRST arm")
    ap.add_argument("--strict", action="store_true",
                    help="with --key-sequence: no duplicate collapsing (W2.5's criterion)")
    ap.add_argument("--only-instances", default="",
                    help="restrict --key-sequence to these instance ids (the judgeable subset)")
    args = ap.parse_args()

    w2 = Path(os.path.expanduser(args.w2_dir))
    logs = ([Path(os.path.expanduser(x)) for x in args.log] if args.log
            else sorted((w2 / "logs").glob("llmgw*.log")))
    lookups = []
    for log in logs:
        lookups.extend(parse_log(log.read_text(errors="ignore")))
    # Rotation means the files are not in chronological order by name alone.
    lookups.sort(key=lambda r: r[0])
    print(f"{len(lookups)} cache lookups in {len(logs)} log file(s)")

    per_arm = {}
    for arm in args.arms:
        meta = json.loads((w2 / "arms" / f"{arm}.json").read_text())
        stats = per_instance(lookups, meta["start"], meta["end"])
        per_arm[arm] = stats
        hit = sum(s["hit"] for s in stats.values())
        total = sum(s["total"] for s in stats.values())
        dirty = sorted(i for i, s in stats.items() if s["miss"])
        print(f"\narm {arm}: {len(stats)} instances, {hit}/{total} requests hit "
              f"({100*hit/total if total else 0:.1f}%), {len(dirty)} instance(s) with misses")
        for iid in dirty:
            s = stats[iid]
            print(f"    {iid:45} hit={s['hit']:>4} miss={s['miss']:>4} rate={100*s['rate']:5.1f}%")
        if args.per_instance:
            for iid in sorted(stats):
                s = stats[iid]
                print(f"  · {iid:45} hit={s['hit']:>4} miss={s['miss']:>4}")

    if args.key_sequence:
        only = None
        if args.only_instances:
            only = {l.strip() for l in
                    Path(os.path.expanduser(args.only_instances)).read_text().splitlines()
                    if l.strip()}
        windows = {arm: json.loads((w2 / "arms" / f"{arm}.json").read_text())
                   for arm in args.arms}
        seqs = {arm: key_sequences(lookups, windows[arm]["start"], windows[arm]["end"])
                for arm in args.arms}
        base = args.arms[0]
        mode = "strict (no collapsing)" if args.strict else "collapsed (consecutive dups dropped)"
        for arm in args.arms[1:]:
            out = compare_key_sequences(seqs[base], seqs[arm], strict=args.strict, only=only)
            print(f"\nkey sequence {arm} vs {base} — {mode}: "
                  f"{len(out['same'])} same, {len(out['differ'])} differ, "
                  f"{len(out['absent'])} not comparable")
            for row in out["rows"]:
                if row["verdict"] != "SAME":
                    print(f"    {row['instance']:45} {row['verdict']} "
                          f"len={row.get('len')} dups={row['dups']}")
            dups = [r["dups"] for r in out["rows"] if r["verdict"] == "SAME" and r["dups"]]
            print(f"    duplicates absorbed: total={sum(dups)} across {len(dups)} instance(s)"
                  + ("  ← must be 0 under W2.5's exactly-once" if args.strict else
                     "  ← compare against Σ step_resume_count"))

    clean = clean_subset(per_arm)
    union = set().union(*(set(s) for s in per_arm.values())) if per_arm else set()
    print(f"\njudgeable subset (100% replay in every arm): {len(clean)} / {len(union)} instances")
    if args.clean_subset:
        Path(os.path.expanduser(args.clean_subset)).write_text("\n".join(clean) + "\n")
        print(f"written to {args.clean_subset}")


if __name__ == "__main__":
    main()
